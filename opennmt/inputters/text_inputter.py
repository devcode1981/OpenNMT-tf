"""Define word-based embedders."""

import abc
import collections
import os
import shutil
import six

import numpy as np
import tensorflow as tf

from tensorflow.contrib.tensorboard.plugins import projector

from google.protobuf import text_format

from opennmt.tokenizers.tokenizer import SpaceTokenizer
from opennmt.inputters.inputter import Inputter
from opennmt.utils.misc import count_lines
from opennmt.constants import PADDING_TOKEN


def visualize_embeddings(log_dir, embedding_var, vocabulary_file, num_oov_buckets):
  """Registers an embedding variable for visualization in TensorBoard.

  This function registers `embedding_var` in the `projector_config.pbtxt`
  file and generates metadata from `vocabulary_file` to attach a label
  to each word ID.

  Args:
    log_dir: The active log directory.
    embedding_var: The embedding variable to visualize.
    vocabulary_file: The associated vocabulary file.
    num_oov_buckets: The number of additional unknown tokens.
  """
  # Copy vocabulary file to log_dir.
  basename = os.path.basename(vocabulary_file)
  destination = os.path.join(log_dir, basename)
  shutil.copy(vocabulary_file, destination)

  # Append <unk> tokens.
  with open(destination, "a") as vocab:
    if num_oov_buckets == 1:
      vocab.write("<unk>\n")
    else:
      for i in range(num_oov_buckets):
        vocab.write("<unk{}>\n".format(i))

  config = projector.ProjectorConfig()

  # If the projector file exists, load it.
  target = os.path.join(log_dir, "projector_config.pbtxt")
  if os.path.exists(target):
    with open(target) as target_file:
      text_format.Merge(target_file.read(), config)

  # If this embedding is already registered, just update the metadata path.
  exists = False
  for meta in config.embeddings:
    if meta.tensor_name == embedding_var.name:
      meta.metadata_path = basename
      exists = True
      break

  if not exists:
    embedding = config.embeddings.add()
    embedding.tensor_name = embedding_var.name
    embedding.metadata_path = basename

  summary_writer = tf.summary.FileWriter(log_dir)

  projector.visualize_embeddings(summary_writer, config)

def load_pretrained_embeddings(embedding_file, vocabulary_file):
  """Returns pretrained embeddings relative to the vocabulary.

  This function will iterate on each embedding in `embedding_file`
  and assign the pretrained vector to the associated word in
  `vocabulary_file` if found. Otherwise, the embedding is ignored.

  Word alignment are case insensitive meaning the pretrained word
  embedding for "the" will be assigned to "the", "The", "THE", or
  any other case variants included in `vocabulary_file`.

  Args:
    embedding_file: Path the embedding file with the format:
      ```
      word1 val1 val2 ... valM
      word2 val1 val2 ... valM
      ...
      wordN val1 val2 ... valM
      ```
      Entries will be matched against `vocabulary_file`.
    vocabulary_file: The vocabulary file containing one word per line.

  Returns:
    A Numpy array of shape `[vocabulary_size, embedding_size]`.
  """
  # Map words to ids from the vocabulary.
  word_to_id = collections.defaultdict(list)
  with open(vocabulary_file) as vocabulary:
    count = 0
    for word in vocabulary:
      word = word.strip().lower()
      word_to_id[word].append(count)
      count += 1

  # Fill pretrained embedding matrix.
  with open(embedding_file) as embedding:
    pretrained = None

    for line in embedding:
      fields = line.strip().split()
      word = fields[0]

      if pretrained is None:
        pretrained = np.random.normal(size=(count + 1, len(fields) - 1))

      # Lookup word in the vocabulary.
      if word in word_to_id:
        ids = word_to_id[word]
        for index in ids:
          pretrained[index] = np.asarray(fields[1:])

  return pretrained

def tokens_to_chars(tokens):
  """Splits a list of tokens into unicode characters.

  This is an in-graph transformation.

  Args:
    tokens: A sequence of tokens.

  Returns:
    A `tf.Tensor` of shape `[sequence_length, max_word_length]`.
  """

  def split_chars(token, max_length, delimiter=" "):
    chars = list(token.decode("utf-8"))
    while len(chars) < max_length:
      chars.append(PADDING_TOKEN)
    return delimiter.join(chars).encode("utf-8")

  def string_len(token):
    return len(token.decode("utf-8"))

  # Get the length of each token.
  lengths = tf.map_fn(
      lambda x: tf.py_func(string_len, [x], [tf.int64]),
      tokens,
      dtype=[tf.int64],
      back_prop=False)

  max_length = tf.reduce_max(lengths)

  # Add a delimiter between each unicode character.
  spaced_chars = tf.map_fn(
      lambda x: tf.py_func(split_chars, [x, max_length], [tf.string]),
      tokens,
      dtype=[tf.string],
      back_prop=False)

  # Split on this delimiter
  chars = tf.map_fn(
      lambda x: tf.string_split(x, delimiter=" ").values,
      spaced_chars,
      dtype=tf.string,
      back_prop=False)

  return chars


@six.add_metaclass(abc.ABCMeta)
class TextInputter(Inputter):
  """An abstract inputter that processes text."""

  def __init__(self, tokenizer=SpaceTokenizer()):
    super(TextInputter, self).__init__()
    self.tokenizer = tokenizer

  def get_length(self, data):
    return data["length"]

  def make_dataset(self, data_file):
    return tf.contrib.data.TextLineDataset(data_file)

  def initialize(self, metadata):
    self.tokenizer.initialize(metadata)

  def _process(self, data):
    """Tokenizes raw text."""
    data = super(TextInputter, self)._process(data)

    if "tokens" not in data:
      text = data["raw"]
      tokens = self.tokenizer(text)
      length = tf.shape(tokens)[0]

      data = self.set_data_field(data, "tokens", tokens, padded_shape=[None])
      data = self.set_data_field(data, "length", length, padded_shape=[])

    return data

  @abc.abstractmethod
  def _get_serving_input(self):
    raise NotImplementedError()

  @abc.abstractmethod
  def _transform_data(self, data, mode):
    raise NotImplementedError()

  @abc.abstractmethod
  def _transform(self, inputs, mode, reuse=None):
    raise NotImplementedError()


class WordEmbedder(TextInputter):
  """Simple word embedder."""

  def __init__(self,
               vocabulary_file_key,
               embedding_size=None,
               embedding_file_key=None,
               trainable=True,
               dropout=0.0,
               tokenizer=SpaceTokenizer()):
    """Initializes the parameters of the word embedder.

    Args:
      vocabulary_file_key: The run configuration key of the vocabulary file
        containing one word per line.
      embedding_size: The size of the resulting embedding.
        If `None`, an embedding file must be provided.
      embedding_file_key: The run configuration key of the embedding file.
        See the `load_pretrained_embeddings` function for details about the
        format and behavior.
      trainable: If `False`, do not optimize embeddings.
      dropout: The probability to drop units in the embedding.
      tokenizer: An optional callable to tokenize the input text.

    Raises:
      ValueError: if neither `embedding_size` nor `embedding_file_key` are set.
    """
    super(WordEmbedder, self).__init__(tokenizer=tokenizer)

    self.vocabulary_file_key = vocabulary_file_key
    self.embedding_size = embedding_size
    self.embedding_file_key = embedding_file_key
    self.trainable = trainable
    self.dropout = dropout
    self.num_oov_buckets = 1

    if embedding_size is None and embedding_file_key is None:
      raise ValueError("Must either provide embedding_size or embedding_file_key")

  def initialize(self, metadata):
    super(WordEmbedder, self).initialize(metadata)
    self.vocabulary_file = metadata[self.vocabulary_file_key]
    self.embedding_file = metadata[self.embedding_file_key] if self.embedding_file_key else None

    self.vocabulary_size = count_lines(self.vocabulary_file) + self.num_oov_buckets
    self.vocabulary = tf.contrib.lookup.index_table_from_file(
        self.vocabulary_file,
        vocab_size=self.vocabulary_size - self.num_oov_buckets,
        num_oov_buckets=self.num_oov_buckets)

  def _get_serving_input(self):
    receiver_tensors = {
        "tokens": tf.placeholder(tf.string, shape=(None, None)),
        "length": tf.placeholder(tf.string, shape=(None))
    }

    features = receiver_tensors.copy()
    features["ids"] = self.vocabulary.lookup(features["tokens"])

    return receiver_tensors, features

  def _process(self, data):
    """Converts words tokens to ids."""
    data = super(WordEmbedder, self)._process(data)

    if "ids" not in data:
      tokens = data["tokens"]
      ids = self.vocabulary.lookup(tokens)

      data = self.set_data_field(data, "ids", ids, padded_shape=[None])

    return data

  def visualize(self, log_dir):
    with tf.variable_scope(tf.get_variable_scope(), reuse=True):
      embeddings = tf.get_variable("w_embs")
      visualize_embeddings(log_dir, embeddings, self.vocabulary_file, self.num_oov_buckets)

  def _transform_data(self, data, mode):
    return self.transform(data["ids"], mode)

  def _transform(self, inputs, mode, reuse=None):
    try:
      embeddings = tf.get_variable("w_embs", trainable=self.trainable)
    except ValueError:
      # Variable does not exist yet.
      if self.embedding_file:
        pretrained = load_pretrained_embeddings(self.embedding_file, self.vocabulary_file)
        self.embedding_size = pretrained.shape[-1]

        shape = None
        initializer = tf.constant(pretrained.astype(np.float32))
      else:
        shape = [self.vocabulary_size, self.embedding_size]
        initializer = None

      embeddings = tf.get_variable(
          "w_embs",
          shape=shape,
          initializer=initializer,
          trainable=self.trainable)

    outputs = tf.nn.embedding_lookup(embeddings, inputs)

    outputs = tf.layers.dropout(
        outputs,
        rate=self.dropout,
        training=mode == tf.estimator.ModeKeys.TRAIN)

    return outputs


class CharConvEmbedder(TextInputter):
  """An inputter that applies a convolution on characters embeddings."""

  def __init__(self,
               vocabulary_file_key,
               embedding_size,
               num_outputs,
               kernel_size=5,
               stride=3,
               dropout=0.0,
               tokenizer=SpaceTokenizer()):
    """Initializes the parameters of the character convolution embedder.

    Args:
      vocabulary_file_key: The run configuration key of the vocabulary file
        containing one character per line.
      embedding_size: The size of the character embedding.
      num_outputs: The dimension of the convolution output space.
      kernel_size: Length of the convolution window.
      stride: Length of the convolution stride.
      dropout: The probability to drop units in the embedding.
      tokenizer: An optional callable to tokenize the input text.
    """
    super(CharConvEmbedder, self).__init__(tokenizer=tokenizer)

    self.vocabulary_file_key = vocabulary_file_key
    self.embedding_size = embedding_size
    self.num_outputs = num_outputs
    self.kernel_size = kernel_size
    self.stride = stride
    self.dropout = dropout
    self.num_oov_buckets = 1

  def initialize(self, metadata):
    super(CharConvEmbedder, self).initialize(metadata)
    self.vocabulary_file = metadata[self.vocabulary_file_key]
    self.vocabulary_size = count_lines(self.vocabulary_file) + self.num_oov_buckets
    self.vocabulary = tf.contrib.lookup.index_table_from_file(
        self.vocabulary_file,
        vocab_size=self.vocabulary_size - self.num_oov_buckets,
        num_oov_buckets=self.num_oov_buckets)

  def _get_serving_input(self):
    receiver_tensors = {
        "chars": tf.placeholder(tf.string, shape=(None, None, None)),
        "length": tf.placeholder(tf.string, shape=(None))
    }

    features = receiver_tensors.copy()
    features["char_ids"] = self.vocabulary.lookup(features["chars"])

    return receiver_tensors, features

  def _process(self, data):
    """Converts words to characters."""
    data = super(CharConvEmbedder, self)._process(data)

    if "char_ids" not in data:
      tokens = data["tokens"]
      chars = tokens_to_chars(tokens)
      ids = self.vocabulary.lookup(chars)

      data = self.set_data_field(data, "char_ids", ids, padded_shape=[None, None])

    return data

  def visualize(self, log_dir):
    with tf.variable_scope(tf.get_variable_scope(), reuse=True):
      embeddings = tf.get_variable("w_char_embs")
      visualize_embeddings(log_dir, embeddings, self.vocabulary_file, self.num_oov_buckets)

  def _transform_data(self, data, mode):
    return self.transform(data["char_ids"], mode)

  def _transform(self, inputs, mode, reuse=None):
    embeddings = tf.get_variable(
        "w_char_embs", shape=[self.vocabulary_size, self.embedding_size])

    outputs = tf.nn.embedding_lookup(embeddings, inputs)
    outputs = tf.layers.dropout(
        outputs,
        rate=self.dropout,
        training=mode == tf.estimator.ModeKeys.TRAIN)

    # Merge batch and sequence timesteps dimensions.
    outputs = tf.reshape(
        outputs,
        [-1, tf.shape(inputs)[-1], self.embedding_size])

    outputs = tf.layers.conv1d(
        outputs,
        self.num_outputs,
        self.kernel_size,
        strides=self.stride)

    # Max pooling over depth.
    outputs = tf.reduce_max(outputs, axis=1)

    # Split batch and sequence timesteps dimensions.
    outputs = tf.reshape(
        outputs,
        [-1, tf.shape(inputs)[1], self.num_outputs])

    return outputs