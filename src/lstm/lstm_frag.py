# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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

"""Example / benchmark for building a PTB LSTM model.

Trains the model described in:
(Zaremba, et. al.) Recurrent Neural Network Regularization
http://arxiv.org/abs/1409.2329

There are 3 supported model configurations:
===========================================
| config | epochs | train | valid  | test
===========================================
| small  | 13     | 37.99 | 121.39 | 115.91
| medium | 39     | 48.45 |  86.16 |  82.07
| large  | 55     | 37.87 |  82.62 |  78.29
The exact results may vary depending on the random initialization.

The hyperparameters used in the model:
- init_scale - the initial scale of the weights
- learning_rate - the initial value of the learning rate
- max_grad_norm - the maximum permissible norm of the gradient
- num_layers - the number of LSTM layers
- num_steps - the number of unrolled steps of LSTM
- hidden_size - the number of LSTM units
- max_epoch - the number of epochs trained with the initial learning rate
- max_max_epoch - the total number of epochs for training
- keep_prob - the probability of keeping weights in the dropout layer
- lr_decay - the decay of the learning rate for each epoch after "max_epoch"
- batch_size - the batch size

The data required for this example is in the data/ dir of the
PTB dataset from Tomas Mikolov's webpage:

$ wget http://www.fit.vutbr.cz/~imikolov/rnnlm/simple-examples.tgz
$ tar xvf simple-examples.tgz

To run:

$ python ptb_word_lm.py --data_path=simple-examples/data/

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
sys.path.insert(0, "../src/")

import inspect
import time
from utils.vector_manager import VectorManager
import subprocess

import numpy as np
import tensorflow as tf

import reader_wp as reader

flags = tf.flags
logging = tf.logging

flags.DEFINE_string(
    "model", "small",
    "A type of model. Possible options are: small, medium, large.")

flags.DEFINE_string(
    "tasks", "all",
    "Tasks to be performed. Possible options are: all, train, test, valid")

flags.DEFINE_string(
    "word_to_id_path", "../models/eos/word2id_1000.pklz",
    "A type of model. Possible options are: small, medium, large.")

flags.DEFINE_string(
    "embeddings", "../models/eos/idWordVec_",
    "Embeddings path")

flags.DEFINE_string("data_path", None,
                    "Where the training/test data is stored.")
flags.DEFINE_string("save_path", None,
                    "Model output directory.")
flags.DEFINE_bool("use_fp16", False,
                  "Train using 16-bit floats instead of 32bit floats")

FLAGS = flags.FLAGS


def data_type():
    return tf.float16 if FLAGS.use_fp16 else tf.float32


def get_vocab_size():
    word_to_id = VectorManager.read_vector(FLAGS.word_to_id_path)
    size = len(word_to_id)
    print("Vocabulary size: %s" % size)
    return size


def generate_arrays_from_list(name, files, embeddings, num_steps=35, batch_size=20, embedding_size=200, n_vocab=126930):

    debug = False
    while 1:
        for file_name in files:
            # print("Generating from file %s for %s" % (file_name, name))
            raw_list = VectorManager.parse_into_list(open(file_name).read())

            n_words = len(raw_list)
            batch_len = n_words // batch_size
            data = np.reshape(raw_list[0:batch_size*batch_len], [batch_size, batch_len])

            for i in range(0, n_words - num_steps, num_steps):

                x = data[0:batch_size, i * num_steps:(i + 1) * num_steps]
                x = [[embeddings[int(elem)][2] for elem in l] for l in x]
                y = data[0:batch_size, i * num_steps + 1:(i + 1) * num_steps + 1]

                if debug:
                    print("Batch size %s\nNum steps %s\nEmbedding size %s" % (batch_size, num_steps, embedding_size
                                                                              ))
                    print("Len(x): %s\n Len(x[0] %s\n Len(x[0][0] %s" % (len(x), len(x[0]), len(x[0][0])))
                    print("Len(y): %s\n Len(y[0] %s" % (len(y), len(y[0])))

                if len(x[0]) < num_steps:
                    break
                x = np.reshape(x, newshape=(batch_size, num_steps, embedding_size))

                y = np.reshape(y, newshape=(batch_size, num_steps))
                # except ValueError as e:
                #     # End of file reached
                #     print("Exception %s" % e)
                #     break
                yield x, y

class WPModel(object):
    """Word Prediction model."""

    def __init__(self, is_training, config):

        self.config = config
        batch_size = config.batch_size
        num_steps = config.num_steps
        size = config.hidden_size
        vocab_size = config.vocab_size
        embedding_size = config.embedding_size

        def lstm_cell():
            # With the latest TensorFlow source code (as of Mar 27, 2017),
            # the BasicLSTMCell will need a reuse parameter which is unfortunately not
            # defined in TensorFlow 1.0. To maintain backwards compatibility, we add
            # an argument check here:
            # if 'reuse' in inspect.getargspec(
            #     tf.contrib.rnn.BasicLSTMCell.__init__).args:
            #   return tf.contrib.rnn.BasicLSTMCell(
            #       size, forget_bias=0.0, state_is_tuple=True,
            #       reuse=tf.get_variable_scope().reuse)
            # else:
            return tf.contrib.rnn.BasicLSTMCell(
                size, forget_bias=0.0, state_is_tuple=True)

        attn_cell = lstm_cell
        if is_training and config.keep_prob < 1:
            def attn_cell():
                return tf.contrib.rnn.DropoutWrapper(
                    lstm_cell(), output_keep_prob=config.keep_prob)

        cell = tf.contrib.rnn.MultiRNNCell(
            [attn_cell() for _ in range(config.num_layers)], state_is_tuple=True)

        # data_type() returns float32 or float16
        self._initial_state = cell.zero_state(batch_size, data_type())

        with tf.device("/cpu:0"):
            # TODO: replace TF input with my embeddings
            # TODO: implement PTB reader or something similar
            # embedding = tf.get_variable(
            #     "embedding", [vocab_size, size], dtype=data_type())
            # embeddings = tf.placeholder(dtype=data_type(), shape=(config.vocab_size, config.embeding_size))
            # inputs = tf.nn.embedding_lookup(embeddings, input_.input_data)
            self.inputs = tf.placeholder(dtype=data_type(), shape=(batch_size, num_steps, embedding_size))
            self.targets = tf.placeholder(dtype=tf.int32, shape=(batch_size, num_steps))

        if is_training and config.keep_prob < 1:
            # Dropout allows to use the net for train and testing
            # See: https://stackoverflow.com/questions/34597316/why-input-is-scaled-in-tf-nn-dropout-in-tensorflow
            # and: http://www.cs.toronto.edu/~rsalakhu/papers/srivastava14a.pdf
            inputs = tf.nn.dropout(self.inputs, config.keep_prob)
        else:
            inputs = self.inputs
        # Simplified version of models/tutorials/rnn/rnn.py's rnn().
        # This builds an unrolled LSTM for tutorial purposes only.
        # In general, use the rnn() or state_saving_rnn() from rnn.py.
        #
        # The alternative version of the code below is:
        #
        inputs = tf.unstack(inputs, num=num_steps, axis=1)

        outputs, state = tf.contrib.rnn.static_rnn(
            cell, inputs, initial_state=self._initial_state)
        # TODO: passing the sequence_length argument will enable to input variable-length tensors

        # outputs = []
        # state = self._initial_state
        # with tf.variable_scope("RNN"):
        #     for time_step in range(num_steps):
        #         if time_step > 0:
        #             tf.get_variable_scope().reuse_variables()
        #         (cell_output, state) = cell(inputs[:, time_step, :], state) # Call (inputs, state)
        #         outputs.append(cell_output)

        # TODO: check why outputs are stacked and resized
        output = tf.reshape(tf.stack(axis=1, values=outputs), [-1, size])
        softmax_w = tf.get_variable(
            "softmax_w", [size, vocab_size], dtype=data_type())
        softmax_b = tf.get_variable("softmax_b", [vocab_size], dtype=data_type())
        logits = tf.matmul(output, softmax_w) + softmax_b
        loss = tf.contrib.legacy_seq2seq.sequence_loss_by_example(
            [logits],
            [tf.reshape(self.targets, [-1])],
            [tf.ones([batch_size * num_steps], dtype=data_type())])
        self._cost = cost = tf.reduce_sum(loss) / batch_size
        self._final_state = state

        if not is_training:
            return

        self._lr = tf.Variable(0.0, trainable=False)
        tvars = tf.trainable_variables()
        grads, _ = tf.clip_by_global_norm(tf.gradients(cost, tvars),
                                          config.max_grad_norm)
        optimizer = tf.train.GradientDescentOptimizer(self._lr)
        self._train_op = optimizer.apply_gradients(
            zip(grads, tvars),
            global_step=tf.contrib.framework.get_or_create_global_step())

        self._new_lr = tf.placeholder(
            tf.float32, shape=[], name="new_learning_rate")
        self._lr_update = tf.assign(self._lr, self._new_lr)

    def assign_lr(self, session, lr_value):
        session.run(self._lr_update, feed_dict={self._new_lr: lr_value})

    @property
    def input(self):
        return self._input

    @property
    def initial_state(self):
        return self._initial_state

    @property
    def cost(self):
        return self._cost

    @property
    def final_state(self):
        return self._final_state

    @property
    def lr(self):
        return self._lr

    @property
    def train_op(self):
        return self._train_op


class SmallConfig(object):
    """Small config."""
    init_scale = 0.1
    learning_rate = 1.0
    max_grad_norm = 5
    num_layers = 1
    num_steps = 20
    hidden_size = 200
    max_epoch = 4
    max_max_epoch = 13
    keep_prob = 1.0
    lr_decay = 0.5
    batch_size = 20
    vocab_size = 126930
    embedding_size = 200
    epoch_size = 1

class MediumConfig(object):
    """Medium config."""
    init_scale = 0.05
    learning_rate = 1.0
    max_grad_norm = 5
    num_layers = 1
    num_steps = 35
    hidden_size = 650
    max_epoch = 6
    max_max_epoch = 39
    keep_prob = 0.5
    lr_decay = 0.8
    batch_size = 20
    vocab_size = 126930
    embedding_size = 200
    epoch_size = 1

class LargeConfig(object):
    """Large config."""
    init_scale = 0.04
    learning_rate = 1.0
    max_grad_norm = 10
    num_layers = 1
    num_steps = 35
    hidden_size = 1024
    max_epoch = 14
    max_max_epoch = 55
    keep_prob = 0.35
    lr_decay = 1 / 1.15
    batch_size = 20
    vocab_size = 126930
    embedding_size = 1000
    epoch_size = 1

class TestConfig(object):
    """Tiny config, for testing."""
    init_scale = 0.1
    learning_rate = 1.0
    max_grad_norm = 1
    num_layers = 1
    num_steps = 2
    hidden_size = 2
    max_epoch = 1
    max_max_epoch = 1
    keep_prob = 1.0
    lr_decay = 0.5
    batch_size = 10
    vocab_size = 126930
    embedding_size = 200
    epoch_size = 1


def run_epoch(session, generator, model, eval_op=None, verbose=False):
    """Runs the model on the given data."""
    start_time = time.time()
    costs = 0.0
    iters = 0
    config = model.config
    state = session.run(model.initial_state)

    fetches = {
        "cost": model.cost,
        "final_state": model.final_state,
    }
    if eval_op is not None:
        fetches["eval_op"] = eval_op

    print("Epoch size starting training %s" % config.epoch_size)
    for step in range(config.epoch_size):
        x, y = generator.next()
        feed_dict = {}
        for i, (c, h) in enumerate(model.initial_state):
            feed_dict[c] = state[i].c
            feed_dict[h] = state[i].h
        # feed_dict["embeddings"] = embeddings
        feed_dict[model.inputs] = x
        feed_dict[model.targets] = y

        vals = session.run(fetches, feed_dict)
        cost = vals["cost"]
        state = vals["final_state"]

        costs += cost
        iters += config.num_steps

        if verbose and step % (config.epoch_size // 10) == 10:
            print("%.3f perplexity: %.3f speed: %.0f wps" %
                  (step * 1.0 / config.epoch_size, np.exp(costs / iters),
                   iters * config.batch_size / (time.time() - start_time)))

    return np.exp(costs / iters)


def get_config():
    if FLAGS.model == "small":
        return SmallConfig()
    elif FLAGS.model == "medium":
        return MediumConfig()
    elif FLAGS.model == "large":
        return LargeConfig()
    elif FLAGS.model == "test":
        return TestConfig()
    else:
        raise ValueError("Invalid model: %s", FLAGS.model)

def get_epoch_size(files, config):
    total = 0
    for file in files:
        file_words = subprocess.check_output(['wc', '-w', file])
        number = file_words.split()[0]
        total += int(number)
    print("Total words %s" % total)
    epoch_size = ((total // config.batch_size) - 1) // config.num_steps # TODO add size wrt data size

    return epoch_size

def main(_):
    if not FLAGS.data_path:
        raise ValueError("Must set --data_path to wiki data directory list")

    # raw_data = reader.wiki_raw_data(FLAGS.data_path, FLAGS.word_to_id_path)
    train_data, valid_data, test_data = None, None, None

    #vocab_size = get_vocab_size()
    vocab_size = 126930

    config = get_config()
    config.vocab_size = vocab_size

    valid_config = config


    eval_config = get_config()
    eval_config.batch_size = 1
    eval_config.num_steps = 1
    eval_config.vocab_size = vocab_size

    embeddings = VectorManager.read_vector("%s%s.pklz" % (FLAGS.embeddings, config.embedding_size))
    files = open(FLAGS.data_path).read().split()

    training_list = files[0:int(0.8 * len(files))]
    validation_list = files[int(0.8 * len(files)):int(0.9 * len(files))]
    testing_list = files[int(0.9 * len(files)):len(files)]

    config.epoch_size = get_epoch_size(training_list, config)
    valid_config.epoch_size = get_epoch_size(validation_list, valid_config)
    eval_config.epoch_size = get_epoch_size(testing_list, eval_config)


    gen_train = generate_arrays_from_list("Train", training_list, embeddings, batch_size=config.batch_size, embedding_size=config.embedding_size,
                                    num_steps=config.num_steps, n_vocab=config.vocab_size)
    gen_valid = generate_arrays_from_list("Validation", validation_list, embeddings, batch_size=config.batch_size, embedding_size=config.embedding_size,
                                    num_steps=config.num_steps, n_vocab=config.vocab_size)
    gen_test = generate_arrays_from_list("Test", testing_list, embeddings, batch_size=config.batch_size, embedding_size=config.embedding_size,
                                    num_steps=config.num_steps, n_vocab=config.vocab_size)

    print("Epoch sizes\n * Training: %s\n * Validation: %s\n * Testing: %s" %
          (config.epoch_size, valid_config.epoch_size, eval_config.epoch_size) )
    with tf.Graph().as_default():
        # Args: [minval, maxval]
        initializer = tf.random_uniform_initializer(-config.init_scale,
                                                    config.init_scale)

        with tf.name_scope("Train"):
            # train_input = WPInput(config=config, data=train_data, name="TrainInput")
            with tf.variable_scope("Model", reuse=None, initializer=initializer):
                m = WPModel(is_training=True, config=config)
            tf.summary.scalar("Training Loss", m.cost)
            tf.summary.scalar("Learning Rate", m.lr)

        with tf.name_scope("Valid"):
            # valid_input = WPInput(config=config, data=valid_data, name="ValidInput")
            with tf.variable_scope("Model", reuse=True, initializer=initializer):
                mvalid = WPModel(is_training=False, config=valid_config)
            tf.summary.scalar("Validation Loss", mvalid.cost)

        with tf.name_scope("Test"):
            # test_input = WPInput(config=eval_config, data=test_data, name="TestInput")
            with tf.variable_scope("Model", reuse=True, initializer=initializer):
                mtest = WPModel(is_training=False, config=eval_config)

        sv = tf.train.Supervisor(logdir=FLAGS.save_path)
        with sv.managed_session() as session:
            for i in range(config.max_max_epoch):
                lr_decay = config.lr_decay ** max(i + 1 - config.max_epoch, 0.0)
                m.assign_lr(session, config.learning_rate * lr_decay)

                print("Epoch: %d Learning rate: %.3f" % (i + 1, session.run(m.lr)))
                train_perplexity = run_epoch(session, generator=gen_train, model=m, eval_op=m.train_op,
                                             verbose=True)
                print("Epoch: %d Train Perplexity: %.3f" % (i + 1, train_perplexity))
                valid_perplexity = run_epoch(session, generator=gen_valid, model=mvalid)
                print("Epoch: %d Valid Perplexity: %.3f" % (i + 1, valid_perplexity))

            test_perplexity = run_epoch(session, generator=gen_test, model=mtest)
            print("Test Perplexity: %.3f" % test_perplexity)

            if FLAGS.save_path:
                print("Saving model to %s." % FLAGS.save_path)
                sv.saver.save(session, FLAGS.save_path, global_step=sv.global_step)


if __name__ == "__main__":
    tf.app.run()