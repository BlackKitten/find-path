from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import logging
from math import ceil
import sys

import numpy as np
import tensorflow as tf

import utils

# VGG mean for standardisation (BGR).
VGG_MEAN = [103.939, 116.779, 123.68]


class FCN16VGG:
    def __init__(self, vgg16_npy_path=None):
        if vgg16_npy_path is None:
            path = sys.modules[self.__class__.__module__].__file__
            path = os.path.abspath(os.path.join(path, os.pardir))
            path = os.path.join(path, "vgg16.npy")
            vgg16_npy_path = path
            logging.info("Load npy file from '%s'.", vgg16_npy_path)
        if not os.path.isfile(vgg16_npy_path):
            logging.error("File '%s' not found.", vgg16_npy_path)
            sys.exit(1)

        self.data_dict = np.load(vgg16_npy_path, encoding='latin1').item()

        self.weight_decay = 5e-4
        print("npy file loaded")

    def build(self, rgb, train=False, num_classes=3, random_init_fc8=False,
              debug=False):
        """Build the VGG model using loaded weights

        Args:
            rgb: image batch tensor.
                Image in rgb shape. Scaled to Interval [0, 255]
            train: bool.
                Whether to build train or inference graph.
            num_classes: int32.
                How many classes should be predicted (by fc8).
            random_init_fc8: bool.
                Whether to initialize fc8 layer randomly.
                Fine-tuning is required in this case.
            debug: bool.
                Whether to print additional debug information.
        """

        # Convert RGB to BGR.
        with tf.name_scope('Processing'):

            red, green, blue = tf.split(rgb, 3, 3)

            bgr = tf.concat([
                blue - VGG_MEAN[0],
                green - VGG_MEAN[1],
                red - VGG_MEAN[2]], axis=3)

            if debug:
                bgr = tf.Print(bgr, [tf.shape(bgr)],
                               message='Shape of input image: ',
                               summarize=4, first_n=1)

        self.conv1_1 = self._conv_layer(bgr, "conv1_1")
        self.conv1_2 = self._conv_layer(self.conv1_1, "conv1_2")
        self.pool1 = self._max_pool(self.conv1_2, 'pool1', debug)

        self.conv2_1 = self._conv_layer(self.pool1, "conv2_1")
        self.conv2_2 = self._conv_layer(self.conv2_1, "conv2_2")
        self.pool2 = self._max_pool(self.conv2_2, 'pool2', debug)

        self.conv3_1 = self._conv_layer(self.pool2, "conv3_1")
        self.conv3_2 = self._conv_layer(self.conv3_1, "conv3_2")
        self.conv3_3 = self._conv_layer(self.conv3_2, "conv3_3")
        self.pool3 = self._max_pool(self.conv3_3, 'pool3', debug)

        self.conv4_1 = self._conv_layer(self.pool3, "conv4_1")
        self.conv4_2 = self._conv_layer(self.conv4_1, "conv4_2")
        self.conv4_3 = self._conv_layer(self.conv4_2, "conv4_3")
        self.pool4 = self._max_pool(self.conv4_3, 'pool4', debug)

        self.conv5_1 = self._conv_layer(self.pool4, "conv5_1")
        self.conv5_2 = self._conv_layer(self.conv5_1, "conv5_2")
        self.conv5_3 = self._conv_layer(self.conv5_2, "conv5_3")
        self.pool5 = self._max_pool(self.conv5_3, 'pool5', debug)

        self.fc6 = self._fc_layer(self.pool5, "fc6")

        if train:
            self.fc6 = tf.nn.dropout(self.fc6, 0.5)

        self.fc7 = self._fc_layer(self.fc6, "fc7")
        if train:
            self.fc7 = tf.nn.dropout(self.fc7, 0.5)

        if random_init_fc8:
            self.score_fr = self._score_layer(self.fc7, "score_fr",
                                              num_classes)
        else:
            self.score_fr = self._fc_layer(self.fc7, "score_fr",
                                           num_classes=num_classes,
                                           relu=False)

        self.pred = tf.argmax(self.score_fr, dimension=3)

        self.upscore2 = self._upscore_layer(self.score_fr,
                                            shape=tf.shape(self.pool4),
                                            num_classes=num_classes,
                                            debug=debug, name='upscore2',
                                            ksize=4, stride=2)

        self.score_pool4 = self._score_layer(self.pool4, "score_pool4",
                                             num_classes=num_classes)

        self.fuse_pool4 = tf.add(self.upscore2, self.score_pool4)

        self.upscore32 = self._upscore_layer(self.fuse_pool4,
                                             shape=tf.shape(bgr),
                                             num_classes=num_classes,
                                             debug=debug, name='upscore32',
                                             ksize=32, stride=16)

        self.pred_up = tf.argmax(self.upscore32, dimension=3)

    def _max_pool(self, input, name, debug):
        """Perform the max pooling on the input.

        Args:
            input: tensor, float32.
            name: string.
                Name of the layer.
            debug: bool.
                Whether to print additional debug information.

        Returns:
            pool: tensor, float32.
        """
        pool = tf.nn.max_pool(input, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1],
                              padding='SAME', name=name)

        if debug:
            pool = tf.Print(pool, [tf.shape(pool)],
                            message='Shape of %s' % name,
                            summarize=4, first_n=1)
        return pool

    def _conv_layer(self, input, name, debug=False):
        """Compute convolution given and filter tensors.

        Args:
            input: tensor, float32.
            name: string.
                Name of the layer.
            debug: bool.
                Whether to print additional debug information.

        Returns:
            relu: tensor, float32.
        """
        with tf.variable_scope(name):
            filter = self._get_conv_filter(name)
            conv = tf.nn.conv2d(input, filter, [1, 1, 1, 1], padding='SAME')

            conv_biases = self._get_bias(name)
            bias = tf.nn.bias_add(conv, conv_biases)

            relu = tf.nn.relu(bias)

            # Add summary to TensorBoard.
            utils.activation_summary(relu)

            if debug:
                relu = tf.Print(bias, [tf.shape(relu)],
                                message='Shape of %s' % name,
                                summarize=4, first_n=1)

            return relu

    def _fc_layer(self, input, name, num_classes=None,
                  relu=True, debug=False):
        """Computes fully-connected given and filter tensors.

        Args:
            input: tensor, float32.
            name: string.
                Name of the layer.
            num_classes: int32.
                How many classes should be predicted.
            relu: bool.
                Whether to computes rectified linear.
            debug: bool.
                Whether to print additional debug information.

        Returns:
            bias: tensor, float32.
        """
        with tf.variable_scope(name):

            if name == 'fc6':
                filter = self._get_fc_weight_reshape(name, [7, 7, 512, 4096])
            elif name == 'score_fr':
                name = 'fc8'  # Name of score_fr layer in VGG model.
                filter = self._get_fc_weight_reshape(name, [1, 1, 4096, 1000],
                                                     num_classes=num_classes)
            else:
                filter = self._get_fc_weight_reshape(name, [1, 1, 4096, 4096])

            conv = tf.nn.conv2d(input, filter, [1, 1, 1, 1], padding='SAME')
            conv_biases = self._get_bias(name, num_classes=num_classes)
            bias = tf.nn.bias_add(conv, conv_biases)

            if relu:
                bias = tf.nn.relu(bias)

            # Add summary to TensorBoard.
            utils.activation_summary(bias)

            if debug:
                bias = tf.Print(bias, [tf.shape(bias)],
                                message='Shape of %s' % name,
                                summarize=4, first_n=1)
            return bias

    def _score_layer(self, input, name, num_classes, debug=False):
        """Get classification scores.

        Args:
            input: tensor, float32.
            name: string.
                Name of the layer.
            num_classes: int32.
                How many classes should be predicted.

        Returns:
            tensor, float32.
        """
        with tf.variable_scope(name):
            # Get number of input channels.
            in_features = input.get_shape()[3].value
            shape = [1, 1, in_features, num_classes]

            # He initialization.
            if name == "score_fr":
                num_input = in_features
                stddev = (2 / num_input) ** 0.5
            elif name == "score_pool4":
                stddev = 0.001

            # Apply convolution.
            weight_decay = self.weight_decay
            weights = self._variable_with_weight_decay(shape, stddev, weight_decay)
            conv = tf.nn.conv2d(input, weights, [1, 1, 1, 1], padding='SAME')

            # Apply bias.
            conv_biases = self._bias_variable([num_classes], constant=0.0)
            bias = tf.nn.bias_add(conv, conv_biases)

            # Add summary to TensorBoard.
            utils.activation_summary(bias)

            if debug:
                bias = tf.Print(bias, [tf.shape(bias)],
                                message='Shape of %s' % name,
                                summarize=4, first_n=1)

            return bias

    def _upscore_layer(self, input, shape,
                       num_classes, name, debug,
                       ksize=4, stride=2):
        """Upsample layer.

        Args:
            input: tensor, float32.
            shape: numpy array
                The shape of the desired layer.
            num_classes: int32.
                How many classes should be predicted.
            name: string.
                Name of the layer.
            debug: bool.
                Whether to print additional debug information.
            ksize: int32.
            stride: inhttps://www.w3schools.com/css/css_float.aspt32.
        Returns:
            deconv: tensor, float32.
                Upsampled layer.
        """
        strides = [1, stride, stride, 1]
        with tf.variable_scope(name):
            in_features = input.get_shape()[3].value

            # Compute shape out of input.
            if shape is None:
                in_shape = tf.shape(input)

                height = ((in_shape[1] - 1) * stride) + 1
                width = ((in_shape[2] - 1) * stride) + 1
                new_shape = [in_shape[0], height, width, num_classes]
            else:
                new_shape = [shape[0], shape[1], shape[2], num_classes]

            output_shape = tf.stack(new_shape)

            logging.debug("Layer: %s, Fan-in: %d" % (name, in_features))
            filter_shape = [ksize, ksize, num_classes, in_features]

            weights = self._get_deconv_filter(filter_shape)
            deconv = tf.nn.conv2d_transpose(input, weights, output_shape,
                                            strides=strides, padding='SAME')

            # Add summary to TensorBoard.
            utils.activation_summary(deconv)

            if debug:
                deconv = tf.Print(deconv, [tf.shape(deconv)],
                                  message='Shape of %s' % name,
                                  summarize=4, first_n=1)

        return deconv

    def _get_deconv_filter(self, filter_shape):
        """Get doconvolutional filter.

        Args:
            filter_shape: list, int32.
        Returns:
            tensor variable.
                Upsampled deconvolutional filter.
        """
        width = filter_shape[0]
        height = filter_shape[0]
        f = ceil(width / 2.0)
        c = (2 * f - 1 - f % 2) / (2.0 * f)

        bilinear = np.zeros([filter_shape[0], filter_shape[1]])

        for x in range(width):
            for y in range(height):
                value = (1 - abs(x / f - c)) * (1 - abs(y / f - c))
                bilinear[x, y] = value

        weights = np.zeros(filter_shape)

        for i in range(filter_shape[2]):
            weights[:, :, i, i] = bilinear

        init = tf.constant_initializer(value=weights,
                                       dtype=tf.float32)

        return tf.get_variable(name="up_filter", initializer=init,
                               shape=weights.shape)

    def _get_conv_filter(self, name):
        """Get convolutional filter.

        Args:
            name: string.
                Name of the layer.
        Returns:
            filter: tensor variable.
                Convolutional filter.
        """
        init = tf.constant_initializer(value=self.data_dict[name][0],
                                       dtype=tf.float32)
        shape = self.data_dict[name][0].shape

        print('Layer name: %s' % name)
        print('Layer shape: %s' % str(shape))

        filter = tf.get_variable(name="filter", initializer=init, shape=shape)

        if not tf.get_variable_scope().reuse:
            weight_decay = tf.multiply(tf.nn.l2_loss(filter), self.weight_decay,
                                       name='weight_loss')
            tf.add_to_collection('losses', weight_decay)

        return filter

    def _get_bias(self, name, num_classes=None):
        """Get bias weights of given layer name.

        Args:
            name: string.
                Name of the layer.
            num_classes: int32.
                How many classes should be predicted.
        Returns:
            biases: tensor variable.
                Bias weights of the layer.
        """
        bias_weights = self.data_dict[name][1]
        shape = self.data_dict[name][1].shape

        if name == 'fc8':
            bias_weights = self._bias_reshape(bias_weights,
                                              shape[0],
                                              num_classes)
            shape = [num_classes]

        init = tf.constant_initializer(value=bias_weights,
                                       dtype=tf.float32)
        biases = tf.get_variable(name="biases", initializer=init, shape=shape)

        return biases

    def _get_fc_weight(self, name):
        """Get weights of given layer name.

        Args:
            name: string.
                Name of the layer.
        Returns:
            Fully-connected weights of the layer.
        """
        init = tf.constant_initializer(value=self.data_dict[name][0],
                                       dtype=tf.float32)
        shape = self.data_dict[name][0].shape
        weights = tf.get_variable(name="weights", initializer=init, shape=shape)

        if not tf.get_variable_scope().reuse:
            weight_decay = tf.multiply(tf.nn.l2_loss(weights), self.weight_decay,
                                       name='weight_loss')
            tf.add_to_collection('losses', weight_decay)

        return weights

    @staticmethod
    def _bias_reshape(bias_weights, num_origin_classes, num_new_classes):
        """Build bias weights for filter results.

        Args:
            bias_weights: numpy array.
                Bias weights (by default for 1000 classes).
            num_origin_classes: int32.
                The number of origin classes.
            num_new_classes: int32.
                The number of new classes.

        Returns:
            averaged_bias_weights: numpy array.
                Bias weights for new classes.
        """
        n_averaged_elements = num_origin_classes // num_new_classes
        averaged_bias_weights = np.zeros(num_new_classes)

        for i in range(0, num_origin_classes, n_averaged_elements):
            start_idx = i
            end_idx = start_idx + n_averaged_elements
            averaged_idx = start_idx // n_averaged_elements
            if averaged_idx == num_new_classes:
                break
            averaged_bias_weights[averaged_idx] = np.mean(bias_weights[start_idx:end_idx])
        return averaged_bias_weights

    @staticmethod
    def _variable_with_weight_decay(shape, stddev, weight_decay):
        """Helper to create an initialized Variable with weight decay.

        Note that the Variable is initialized with a truncated normal
        distribution.
        A weight decay is added only if one is specified.

        Args:
            shape: list, int32.
            stddev: float32.
                standard deviation of a truncated Gaussian.
            weight_decay: tensor, float32.
                Result of addition of L2Loss weight decay multiplied by this float. If None, weight
                decay is not added for this Variable.

        Returns:
             weights: variable tensor.
        """

        initializer = tf.truncated_normal_initializer(stddev=stddev)
        weights = tf.get_variable('weights',
                                  shape=shape,
                                  initializer=initializer)

        if weight_decay and (not tf.get_variable_scope().reuse):
            weight_decay = tf.multiply(
                tf.nn.l2_loss(weights), weight_decay, name='weight_loss')
            tf.add_to_collection('losses', weight_decay)

        return weights

    def _bias_variable(self, shape, constant=0.0):
        initializer = tf.constant_initializer(constant)
        return tf.get_variable(name='biases', shape=shape,
                               initializer=initializer)

    def _get_fc_weight_reshape(self, name, shape, num_classes=None):
        """GET fully-connected layer reshaped (from default).

        Args:
            name: string.
                Name of the layer.
            shape: numpy array
                The shape of the desired layer.
            num_classes: int32.
                How many classes should be predicted.

        Returns:
            weights: variable tensor.
        """
        print('Layer name: %s' % name)
        print('Layer shape: %s' % shape)
        weights = self.data_dict[name][0]
        weights = weights.reshape(shape)

        if num_classes is not None:
            weights = self._summary_reshape(weights, shape, num_classes)

        init = tf.constant_initializer(value=weights,
                                       dtype=tf.float32)
        weights = tf.get_variable(name="weights", initializer=init, shape=shape)

        return weights

    def _summary_reshape(self, fcn_weights, shape, num_new_classes):
        """Produce weights for a reduced fully-connected layer.

        FC8 of VGG produces 1000 classes. Most semantic segmentation
        task require much less classes. This reshapes the original weights
        which are used in a fully-convolutional layer and produces num_new
        classes. To give the average (mean) for new array of the new classes.

        Consider reordering fcn_weights, to preserve semantic meaning of the
        weights.

        Args:
            fcn_weights: numpy array.
                Original FCN weights (by default for 1000 classes).
            shape: numpy array
                The shape of the desired layer.
            num_new_classes: int32.
                The number of new classes.

        Returns:
            averaged_fcn_weights: numpy array.
                Filter weights for new classes.
        """
        num_origin_classes = shape[3]

        # [:, :, origin (] => [:, :, num_new]
        shape[3] = num_new_classes

        assert (num_new_classes < num_origin_classes)
        n_averaged_elements = num_origin_classes // num_new_classes
        averaged_fcn_weights = np.zeros(shape)

        # TODO optimise this 'for' statement.
        for i in range(0, num_origin_classes, n_averaged_elements):
            start_idx = i
            end_idx = start_idx + n_averaged_elements
            averaged_idx = start_idx // n_averaged_elements
            if averaged_idx == num_new_classes:
                break
            averaged_fcn_weights[:, :, :, averaged_idx] = np.mean(
                fcn_weights[:, :, :, start_idx:end_idx], axis=3)
        return averaged_fcn_weights
