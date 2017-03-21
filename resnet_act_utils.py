# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Utility functions for ResNet with adaptive computation time (ACT)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial

import h5py
import tensorflow as tf

from tensorflow.contrib import slim

import act
import flopsometer


SACT_KERNEL_SIZE = 3
INIT_BIAS = -3.


def add_all_ponder_costs(end_points, weights):
  total_ponder_cost = 0.
  for scope in end_points['block_scopes']:
    ponder_cost = end_points['{}/ponder_cost'.format(scope)]
    total_ponder_cost += tf.reduce_mean(ponder_cost)
  tf.losses.add_loss(total_ponder_cost * weights)


def moments_metric_map(x, name, delimiter='_', do_shift=False):
  tf.summary.histogram(name, x)

  if do_shift:
    shift = tf.reduce_mean(x)  # Seems to help numerical issues, but slower
  else:
    shift = None

  mean, var = tf.nn.moments(x, range(len(x.get_shape().as_list())),
                            shift=shift)
  metric_map = {
      '{}{}mean'.format(name, delimiter): mean,
      '{}{}std'.format(name, delimiter): tf.sqrt(tf.maximum(0.0, var))
  }
  return metric_map


def act_metric_map(end_points, mean_metric):
  """Assembles ACT-specific metrics into a map for use in slim.metrics."""
  metric_map = {}

  for block_scope in end_points['block_scopes']:
    name = '{}/ponder_cost'.format(block_scope)
    ponder_cost = end_points[name]
    ponder_map = moments_metric_map(ponder_cost, name)
    metric_map.update(ponder_map)

    name = '{}/num_units'.format(block_scope)
    num_units = tf.to_float(end_points[name])
    num_units_map = moments_metric_map(num_units, name)
    metric_map.update(num_units_map)

    name = '{}/num_units_executed'.format(block_scope)
    metric_map[name] = tf.reduce_max(num_units)

  if mean_metric:
    metric_map = {k: slim.metrics.streaming_mean(v)
                  for k, v in metric_map.iteritems()}

  return metric_map


def flops_metric_map(end_points, mean_metric, total_name='Total Flops'):
  """Assembles flops-count metrics into a map for use in slim.metrics."""
  metric_map = {}
  total_flops = tf.to_float(end_points['flops'])
  flops_map = moments_metric_map(total_flops, total_name, delimiter='/',
                                 do_shift=True)
  metric_map.update(flops_map)

  for block_scope in end_points['block_scopes']:
    name = '{}/flops'.format(block_scope)
    flops = tf.to_float(end_points[name])
    flops_map = moments_metric_map(flops, name, do_shift=True)
    metric_map.update(flops_map)

  if mean_metric:
    metric_map = {k: slim.metrics.streaming_mean(v)
                  for k, v in metric_map.iteritems()}

  return metric_map


def get_halting_proba(outputs):
  with tf.variable_scope('halting_proba'):
    x = outputs
    x = tf.reduce_mean(x, [1, 2], keep_dims=True)

    x = slim.batch_norm(x, scope='global_bn')
    halting_proba, flops = flopsometer.conv2d(
        x,
        1,
        1,
        activation_fn=tf.nn.sigmoid,
        normalizer_fn=None,
        biases_initializer=tf.constant_initializer(INIT_BIAS),
        scope='global_conv')
    halting_proba = tf.squeeze(halting_proba, [1, 2])

    return halting_proba, flops


def get_halting_proba_conv(outputs, residual_mask=None):
  with tf.variable_scope('halting_proba'):
    flops = 0

    x = outputs

    local_feature = slim.batch_norm(x, scope='local_bn')
    halting_logit, current_flops = flopsometer.conv2d(
        local_feature,
        1,
        SACT_KERNEL_SIZE,
        activation_fn=None,
        normalizer_fn=None,
        biases_initializer=tf.constant_initializer(INIT_BIAS),
        output_mask=residual_mask,
        scope='local_conv')
    flops += current_flops

    # Add global halting logit.
    global_feature = tf.reduce_mean(x, [1, 2], keep_dims=True)
    global_feature = slim.batch_norm(global_feature, scope='global_bn')
    halting_logit_global, current_flops = flopsometer.conv2d(
        global_feature,
        1,
        1,
        activation_fn=None,
        normalizer_fn=None,
        biases_initializer=None,  # biases are already present in local logits
        scope='global_conv')
    flops += current_flops

    # Addition with broadcasting over spatial dimensions.
    halting_logit += halting_logit_global

    halting_proba = tf.sigmoid(halting_logit)

    return halting_proba, flops


def unit_act(block,
              inputs,
              unit_idx,
              skip_halting_proba=False,
              sact=False,
              residual_mask=None):
  with tf.variable_scope('unit_%d' % (unit_idx + 1), [inputs]):
    outputs, flops = block.unit_fn(
        inputs, *block.args[unit_idx], residual_mask=residual_mask)

    if not skip_halting_proba and unit_idx < len(block.args) - 1:
      if sact:
        halting_proba, current_flops = get_halting_proba_conv(outputs)
        flops += current_flops
      else:
        halting_proba, current_flops = get_halting_proba(outputs)
        flops += current_flops
    else:
      halting_proba = None

    return outputs, halting_proba, flops


def stack_blocks(net, blocks, use_act=False, act_early_stopping=False,
                 sact=False, end_points=None):
  """Utility function for assembling SACT models consisting of 'blocks.'"""
  if end_points is None:
    end_points = {}
  end_points['flops'] = end_points.get('flops', 0)
  end_points['block_scopes'] = [block.scope for block in blocks]
  end_points['block_num_units'] = [len(block.args) for block in blocks]

  for block in blocks:
    if use_act:
      if sact:
        act_func = act.spatially_adaptive_computation_time
      else:
        if act_early_stopping:
          act_func = act.adaptive_computation_early_stopping
        else:
          act_func = act.adaptive_computation_time_wrapper

      (ponder_cost, num_units, flops, halting_distribution, net) = act_func(
          net,
          partial(
              unit_act, block, sact=sact),
          len(block.args),
          scope=block.scope)

      end_points['{}/ponder_cost'.format(block.scope)] = ponder_cost
      end_points['{}/num_units'.format(block.scope)] = num_units
      end_points['{}/halting_distribution'.format(
          block.scope)] = halting_distribution
    else:
      with tf.variable_scope(block.scope, 'block', [net]):
        flops = 0
        for unit_idx in range(len(block.args)):
          net, _, current_flops = unit_act(
              block, net, unit_idx, skip_halting_proba=True)
          flops += current_flops

    end_points['{}/flops'.format(block.scope)] = flops
    end_points['flops'] += flops
    end_points[block.scope] = net

  return net, end_points


def variables_to_str(variables):
  return ', '.join([var.op.name for var in variables])


def get_finetuning_settings(finetune_path, lr_coeff=1.0):
  """Sets up fine-tuning of a SACT model."""
  if not finetune_path:
    return (None, None)

  tf.logging.warning('Finetuning from {}'.format(finetune_path))
  variables = slim.get_model_variables()
  variables_to_restore = [
      var for var in variables if '/halting_proba/' not in var.op.name
  ]
  variables_to_train_fast = [
      var for var in variables if '/halting_proba/' in var.op.name
  ]
  tf.logging.info('Restoring variables: {}'.format(
      variables_to_str(variables_to_restore)))
  tf.logging.info('Training with {}x LR: {}'.format(
      lr_coeff, variables_to_str(variables_to_train_fast)))
  init_fn = slim.assign_from_checkpoint_fn(finetune_path, variables_to_restore)
  gradient_multipliers = {var: lr_coeff for var in variables_to_train_fast}

  return (init_fn, gradient_multipliers)


def sact_image_heatmap(end_points,
                           metric_name,
                           num_images=5,
                           alpha=0.75,
                           border=5,
                           normalize_images=True):
  """Overlays a heatmap of the ponder cost onto the input image."""
  assert metric_name in ('ponder_cost', 'num_units')

  images = end_points['inputs']
  if num_images is not None:
    images = images[:num_images, :, :, :]
  else:
    num_images = tf.shape(images)[0]

  # Normalize the images
  if normalize_images:
    images -= tf.reduce_min(images, [1, 2, 3], True)
    images /= tf.reduce_max(images, [1, 2, 3], True)

  resolution = tf.shape(images)[1:3]

  max_value = sum(end_points['block_num_units'])
  if metric_name == 'ponder_cost':
    max_value += len(end_points['block_num_units'])

  heatmaps = []
  for scope in end_points['block_scopes']:
    h = end_points['{}/{}'.format(scope, metric_name)]
    h = tf.to_float(h)
    h = h[:num_images, :, :]
    h = tf.expand_dims(h, 3)
    # The metric maps can be lower resolution than the image.
    # We simply resize the map to the image size.
    h = tf.image.resize_nearest_neighbor(h, resolution, align_corners=False)
    # Heatmap is in Red channel. Fill Blue and Green channels with zeros.
    dimensions = tf.stack([num_images, resolution[0], resolution[1], 2])
    h = tf.concat([h, tf.zeros(dimensions)], 3)
    heatmaps.append(h)

  im_heatmap = images * (1 - alpha) + tf.add_n(heatmaps) * (alpha / max_value)

  # image, black border, image with overlayed heatmap
  dimensions = tf.stack([num_images, resolution[0], border, 3])
  ret = tf.concat([images, tf.zeros(dimensions), im_heatmap], 2)

  return ret


def add_heatmaps_image_summary(end_points, num_images=3, alpha=0.75, border=5):
  tf.summary.image(
      'heatmaps/ponder_cost',
      sact_image_heatmap(
          end_points,
          'ponder_cost',
          num_images=num_images,
          alpha=alpha,
          border=border))
  tf.summary.image(
      'heatmaps/num_units',
      sact_image_heatmap(
          end_points,
          'num_units',
          num_images=num_images,
          alpha=alpha,
          border=border))


def sact_map(end_points, metric_name):
  """Generates a heatmap of the ponder cost for visualization."""
  assert metric_name in ('ponder_cost', 'num_units')

  inputs = end_points['inputs']
  if inputs.get_shape().is_fully_defined():
    sh = inputs.get_shape().as_list()
  else:
    sh = tf.shape(inputs)
  resolution = sh[1:3]

  heatmaps = []
  for scope in end_points['block_scopes']:
    h = end_points['{}/{}'.format(scope, metric_name)]
    h = tf.to_float(h)
    h = tf.expand_dims(h, 3)
    # The metric maps can be lower resolution than the image.
    # We simply resize the map to the image size.
    h = tf.image.resize_nearest_neighbor(h, resolution, align_corners=False)
    heatmaps.append(h)

  return tf.add_n(heatmaps)


def export_to_h5(checkpoint_path, export_path, images, end_points, num_samples,
                 batch_size, sact):
  """Exports ponder cost maps and other useful info to an HDF5 file."""
  output_file = h5py.File(export_path, 'w')

  output_file.attrs['block_scopes'] = end_points['block_scopes']
  keys_to_tensors = {}
  for block_scope in end_points['block_scopes']:
    for k in ('{}/ponder_cost'.format(block_scope),
              '{}/num_units'.format(block_scope),
              '{}/halting_distribution'.format(block_scope),
              '{}/flops'.format(block_scope)):
      keys_to_tensors[k] = end_points[k]
  keys_to_tensors['images'] = images
  keys_to_tensors['flops'] = end_points['flops']

  if sact:
    keys_to_tensors['ponder_cost_map'] = sact_map(end_points, 'ponder_cost')
    keys_to_tensors['num_units_map'] = sact_map(end_points, 'num_units')

  keys_to_datasets = {}
  for key, tensor in keys_to_tensors.iteritems():
    sh = tensor.get_shape().as_list()
    sh[0] = num_samples
    print(key, sh)
    keys_to_datasets[key] = output_file.create_dataset(
        key, sh, compression='lzf')

  variables_to_restore = slim.get_model_variables()
  init_fn = slim.assign_from_checkpoint_fn(checkpoint_path,
                                           variables_to_restore)

  sv = tf.train.Supervisor(
      graph=tf.get_default_graph(),
      logdir=None,
      summary_op=None,
      summary_writer=None,
      global_step=None,
      saver=None)

  assert num_samples % batch_size == 0
  num_batches = num_samples // batch_size

  with sv.managed_session('', start_standard_services=False) as sess:
    init_fn(sess)
    sv.start_queue_runners(sess)

    for i in range(num_batches):
      tf.logging.info('Evaluating batch %d/%d', i + 1, num_batches)
      end_points_out = sess.run(keys_to_tensors)
      for key, dataset in keys_to_datasets.iteritems():
        dataset[i * batch_size:(i + 1) * batch_size, ...] = end_points_out[key]


def split_and_int(s):
  return [int(x) for x in s.split('_')]
