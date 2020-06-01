# -*- coding: utf-8 -*-
"""transformer_contextual_lenses.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/16NcwXKxsWDshonpEciQox8I3dSDgEH9w

# Initialization
"""

# *** Comment out on cloud ***
# Install the newest JAX and FLAX versions.
!pip install --upgrade -q jax==0.1.61 jaxlib==0.1.42 flax==0.1.0rc2

# *** Comment out on cloud ***
import requests
import os
if 'TPU_DRIVER_MODE' not in globals():
  url = 'http://' + os.environ['COLAB_TPU_ADDR'].split(':')[0] + ':8475/requestversion/tpu_driver_nightly'
  resp = requests.post(url)
  TPU_DRIVER_MODE = 1

# The following is required to use TPU Driver as JAX's backend.
from jax.config import config
config.FLAGS.jax_xla_backend = "tpu_driver"
config.FLAGS.jax_backend_target = "grpc://" + os.environ['COLAB_TPU_ADDR']
print(config.FLAGS.jax_backend_target)

import functools
import itertools
import os
import time

import flax
from flax import jax_utils
from flax import nn
from flax import optim
from flax.metrics import tensorboard
from flax.training import checkpoints
from flax.training import common_utils

import jax
from jax import random
from jax import lax
import jax.nn
import jax.numpy as jnp

import numpy as np

import matplotlib.pyplot as plt

"""# Transformer model
Code source: https://github.com/google/flax/blob/master/examples/lm1b/models.py
"""

def shift_right(x):
  """Shift the input to the right by padding on axis 1."""
  pad_widths = [(0, 0)] * len(x.shape)
  pad_widths[1] = (1, 0)  # Padding on axis=1
  padded = jnp.pad(
      x, pad_widths, mode='constant', constant_values=x.dtype.type(0))
  return padded[:, :-1]

class Embed(nn.Module):
  """Embedding Module.
  A parameterized function from integers [0, n) to d-dimensional vectors.
  """

  def apply(self,
            inputs,
            num_embeddings,
            features,
            mode='input',
            emb_init=nn.initializers.normal(stddev=1.0)):
    """Applies Embed module.
    Args:
      inputs: input data
      num_embeddings: number of embedding
      features: size of the embedding dimension
      mode: either 'input' or 'output' -> to share input/output embedding
      emb_init: embedding initializer
    Returns:
      output which is embedded input data
    """
    embedding = self.param('embedding', (num_embeddings, features), emb_init)
    if mode == 'input':
      if inputs.dtype not in [jnp.int32, jnp.int64, jnp.uint32, jnp.uint64]:
        raise ValueError('Input type must be an integer or unsigned integer.')
      return jnp.take(embedding, inputs, axis=0)
    if mode == 'output':
      return jnp.einsum('bld,vd->blv', inputs, embedding)

def sinusoidal_init(max_len=2048):
  """1D Sinusoidal Position Embedding Initializer.
  Args:
      max_len: maximum possible length for the input
  Returns:
      output: init function returning `(1, max_len, d_feature)`
  """

  def init(key, shape, dtype=np.float32):
    """Sinusoidal init."""
    del key, dtype
    d_feature = shape[-1]
    pe = np.zeros((max_len, d_feature), dtype=np.float32)
    position = np.arange(0, max_len)[:, np.newaxis]
    div_term = np.exp(
        np.arange(0, d_feature, 2) * -(np.log(10000.0) / d_feature))
    pe[:, 0::2] = np.sin(position * div_term)
    pe[:, 1::2] = np.cos(position * div_term)
    pe = pe[np.newaxis, :, :]  # [1, max_len, d_feature]
    return jnp.array(pe)

  return init

class AddPositionEmbs(nn.Module):
  """Adds learned positional embeddings to the inputs."""

  def apply(self,
            inputs,
            max_len=2048,
            posemb_init=nn.initializers.normal(stddev=1.0),
            cache=None):
    """Applies AddPositionEmbs module.
    Args:
      inputs: input data
      max_len: maximum possible length for the input
      posemb_init: positional embedding initializer
      cache: flax attention cache for fast decoding.
    Returns:
      output: `(bs, timesteps, in_dim)`
    """
    assert inputs.ndim == 3, ('Number of dimensions should be 3,'
                              ' but it is: %d' % inputs.ndim)
    length = inputs.shape[1]
    pos_emb_shape = (1, max_len, inputs.shape[-1])
    pos_embedding = self.param('pos_embedding', pos_emb_shape, posemb_init)
    pe = pos_embedding[:, :length, :]
    # We abuse the same attention Cache mechanism to run positional embeddings
    # in fast predict mode. We could use state variables instead, but this
    # simplifies invocation with a single top-level cache context manager.
    # We only use the cache's position index for tracking decoding position.
    if cache:
      if self.is_initializing():
        cache.store(lambda: (4, (1, 1)))
      else:
        cache_entry = cache.retrieve(None)
        i = cache_entry.i
        one = jnp.array(1, jnp.uint32)
        cache_entry = cache_entry.replace(i=cache_entry.i + one)
        cache.store(cache_entry)
        _, _, df = pos_embedding.shape
        pe = lax.dynamic_slice(pos_embedding, jnp.array((0, i, 0)),
                               jnp.array((1, 1, df)))
    return inputs + pe

class MlpBlock(nn.Module):
  """Transformer MLP block."""

  def apply(self,
            inputs,
            mlp_dim,
            out_dim=None,
            dropout_rate=0.1,
            deterministic=False,
            kernel_init=nn.initializers.xavier_uniform(),
            bias_init=nn.initializers.normal(stddev=1e-6)):
    """Applies Transformer MlpBlock module."""
    actual_out_dim = inputs.shape[-1] if out_dim is None else out_dim
    x = nn.Dense(inputs, mlp_dim, kernel_init=kernel_init, bias_init=bias_init)
    x = nn.gelu(x)
    x = nn.dropout(x, rate=dropout_rate, deterministic=deterministic)
    output = nn.Dense(
        x, actual_out_dim, kernel_init=kernel_init, bias_init=bias_init)
    output = nn.dropout(output, rate=dropout_rate, deterministic=deterministic)
    return output

class Transformer1DBlock(nn.Module):
  """Transformer layer (https://openreview.net/forum?id=H1e5GJBtDr)."""

  def apply(self,
            inputs,
            qkv_dim,
            mlp_dim,
            num_heads,
            causal_mask=False,
            padding_mask=None,
            dropout_rate=0.1,
            attention_dropout_rate=0.1,
            deterministic=False,
            cache=None):
    """Applies Transformer1DBlock module.
    Args:
      inputs: input data
      qkv_dim: dimension of the query/key/value
      mlp_dim: dimension of the mlp on top of attention block
      num_heads: number of heads
      causal_mask: bool, mask future or not
      padding_mask: bool, mask padding tokens
      dropout_rate: dropout rate
      attention_dropout_rate: dropout rate for attention weights
      deterministic: bool, deterministic or not (to apply dropout)
      cache: flax autoregressive cache for fast decoding.
    Returns:
      output after transformer block.
    """

    # Attention block.
    assert inputs.ndim == 3
    x = nn.LayerNorm(inputs)
    x = nn.SelfAttention(
        x,
        num_heads=num_heads,
        qkv_features=qkv_dim,
        attention_axis=(1,),
        causal_mask=causal_mask,
        padding_mask=padding_mask,
        kernel_init=nn.initializers.xavier_uniform(),
        bias_init=nn.initializers.normal(stddev=1e-6),
        bias=False,
        broadcast_dropout=False,
        dropout_rate=attention_dropout_rate,
        deterministic=deterministic,
        cache=cache)
    x = nn.dropout(x, rate=dropout_rate, deterministic=deterministic)
    x = x + inputs

    # MLP block.
    y = nn.LayerNorm(x)
    y = MlpBlock(
        y,
        mlp_dim=mlp_dim,
        dropout_rate=dropout_rate,
        deterministic=deterministic)

    return x + y



"""# Hyperparameters"""

num_train_steps = 500000      # Max number of training steps.
eval_frequency = 1000         # How often to run model evaluation.
num_eval_steps = 20           # Number of steps to take during evaluation.
random_seed = 0               # JAX PRNG random seed.
learning_rate = 0.05          # Base learning rate.
weight_decay = 1e-1           # AdamW-style relative weight decay factor.
batch_size = 256              # "Target" Batch size.
max_target_length = 256       # Maximum input length.
max_eval_target_length = 256  # Maximum eval-set input length.

lm_emb_dim = 512              # LM initial token embedding dimension.
lm_num_heads = 8              # Number of heads in decoder layers.
lm_num_layers = 6             # Number of decoder layers.
lm_qkv_dim = 512              # Decoder query/key/value depth.
lm_mlp_dim = 2048             # Feedforward (MLP) layer depth.

rep_size = 256                 # Size of learned linear representation

# Init PRNG Stream.
rng = random.PRNGKey(random_seed)
rng, init_rng = random.split(rng)
# We init the first set of dropout PRNG keys, but update it afterwards inside
# the main pmap'd training update for performance.
dropout_rngs = random.split(rng, jax.local_device_count())

"""# Transformer language model
Code source: https://github.com/google/flax/blob/master/examples/lm1b/models.py
"""

class TransformerLM(nn.Module):
  """Transformer Model for language modeling."""

  def apply(self,
            inputs,
            vocab_size,
            emb_dim=512,
            num_heads=8,
            num_layers=6,
            qkv_dim=512,
            mlp_dim=2048,
            max_len=2048,
            train=False,
            shift=True,
            dropout_rate=0.1,
            attention_dropout_rate=0.1,
            cache=None):
    """Applies Transformer model on the inputs.
    Args:
      inputs: input data
      vocab_size: size of the vocabulary
      emb_dim: dimension of embedding
      num_heads: number of heads
      num_layers: number of layers
      qkv_dim: dimension of the query/key/value
      mlp_dim: dimension of the mlp on top of attention block
      max_len: maximum length.
      train: bool: if model is training.
      shift: bool: if we right-shift input - this is only disabled for
        fast, looped single-token autoregressive decoding.
      dropout_rate: dropout rate
      attention_dropout_rate: dropout rate for attention weights
      cache: flax autoregressive cache for fast decoding.
    Returns:
      output of a transformer decoder.
    """
    padding_mask = jnp.where(inputs > 0, 1, 0).astype(jnp.float32)[..., None]
    assert inputs.ndim == 2  # (batch, len)
    x = inputs
    if shift:
      x = shift_right(x)
    x = x.astype('int32')

    x = Embed(x, num_embeddings=vocab_size, features=emb_dim, name='embed')

    x = AddPositionEmbs(
        x, max_len=max_len, posemb_init=sinusoidal_init(max_len=max_len),
        cache=cache)

    x = nn.dropout(x, rate=dropout_rate, deterministic=not train)

    for _ in range(num_layers):
      x = Transformer1DBlock(
          x,
          qkv_dim=qkv_dim,
          mlp_dim=mlp_dim,
          num_heads=num_heads,
          causal_mask=True,
          padding_mask=padding_mask,
          dropout_rate=dropout_rate,
          attention_dropout_rate=attention_dropout_rate,
          deterministic=not train,
          cache=cache,
      )

    x = nn.LayerNorm(x)

    logits = nn.Dense(
        x,
        vocab_size,
        kernel_init=nn.initializers.xavier_uniform(),
        bias_init=nn.initializers.normal(stddev=1e-6))

    return logits



"""# Transformer lenses
Based on: https://arxiv.org/pdf/2002.08866.pdf

### Lens 1: Pooling

Mean pooling
"""

class TransformerMeanPool(nn.Module):
  """Transformer Model + mean pooling for representations."""

  def apply(self,
            inputs,
            vocab_size,
            emb_dim=512,
            num_heads=8,
            num_layers=6,
            qkv_dim=512,
            mlp_dim=2048,
            max_len=2048,
            train=False,
            shift=True,
            dropout_rate=0.1,
            attention_dropout_rate=0.1,
            cache=None):
    """Applies Transformer model on the inputs.
    Args:
      inputs: input data
      vocab_size: size of the vocabulary
      emb_dim: dimension of embedding
      num_heads: number of heads
      num_layers: number of layers
      qkv_dim: dimension of the query/key/value
      mlp_dim: dimension of the mlp on top of attention block
      max_len: maximum length.
      train: bool: if model is training.
      shift: bool: if we right-shift input - this is only disabled for
        fast, looped single-token autoregressive decoding.
      dropout_rate: dropout rate
      attention_dropout_rate: dropout rate for attention weights
      cache: flax autoregressive cache for fast decoding.
    Returns:
      output of a transformer decoder.
    """
    padding_mask = jnp.where(inputs > 0, 1, 0).astype(jnp.float32)[..., None]
    assert inputs.ndim == 2  # (batch, len)
    x = inputs
    if shift:
      x = shift_right(x)
    x = x.astype('int32')

    x = Embed(x, num_embeddings=vocab_size, features=emb_dim, name='embed')

    x = AddPositionEmbs(
        x, max_len=max_len, posemb_init=sinusoidal_init(max_len=max_len),
        cache=cache)

    x = nn.dropout(x, rate=dropout_rate, deterministic=not train)

    for _ in range(num_layers):
      x = Transformer1DBlock(
          x,
          qkv_dim=qkv_dim,
          mlp_dim=mlp_dim,
          num_heads=num_heads,
          causal_mask=True,
          padding_mask=padding_mask,
          dropout_rate=dropout_rate,
          attention_dropout_rate=attention_dropout_rate,
          deterministic=not train,
          cache=cache,
      )

    x = nn.LayerNorm(x)

    rep = jnp.mean(x, axis=1)

    return rep



"""Max pooling"""

class TransformerMaxPool(nn.Module):
  """Transformer Model + max pooling for representations."""

  def apply(self,
            inputs,
            vocab_size,
            emb_dim=512,
            num_heads=8,
            num_layers=6,
            qkv_dim=512,
            mlp_dim=2048,
            max_len=2048,
            train=False,
            shift=True,
            dropout_rate=0.1,
            attention_dropout_rate=0.1,
            cache=None):
    """Applies Transformer model on the inputs.
    Args:
      inputs: input data
      vocab_size: size of the vocabulary
      emb_dim: dimension of embedding
      num_heads: number of heads
      num_layers: number of layers
      qkv_dim: dimension of the query/key/value
      mlp_dim: dimension of the mlp on top of attention block
      max_len: maximum length.
      train: bool: if model is training.
      shift: bool: if we right-shift input - this is only disabled for
        fast, looped single-token autoregressive decoding.
      dropout_rate: dropout rate
      attention_dropout_rate: dropout rate for attention weights
      cache: flax autoregressive cache for fast decoding.
    Returns:
      output of a transformer decoder.
    """
    padding_mask = jnp.where(inputs > 0, 1, 0).astype(jnp.float32)[..., None]
    assert inputs.ndim == 2  # (batch, len)
    x = inputs
    if shift:
      x = shift_right(x)
    x = x.astype('int32')

    x = Embed(x, num_embeddings=vocab_size, features=emb_dim, name='embed')

    x = AddPositionEmbs(
        x, max_len=max_len, posemb_init=sinusoidal_init(max_len=max_len),
        cache=cache)

    x = nn.dropout(x, rate=dropout_rate, deterministic=not train)

    for _ in range(num_layers):
      x = Transformer1DBlock(
          x,
          qkv_dim=qkv_dim,
          mlp_dim=mlp_dim,
          num_heads=num_heads,
          causal_mask=True,
          padding_mask=padding_mask,
          dropout_rate=dropout_rate,
          attention_dropout_rate=attention_dropout_rate,
          deterministic=not train,
          cache=cache,
      )

    x = nn.LayerNorm(x)

    rep = jnp.max(x, axis=1)

    return rep



"""### Lens 2: Linear + ReLU + Max Pooling"""

class TransformerLinearMaxPool(nn.Module):
  """Transformer Model + linear layer + max pooling for representations."""

  def apply(self,
            inputs,
            vocab_size,
            rep_size=256,
            emb_dim=512,
            num_heads=8,
            num_layers=6,
            qkv_dim=512,
            mlp_dim=2048,
            max_len=2048,
            train=False,
            shift=True,
            dropout_rate=0.1,
            attention_dropout_rate=0.1,
            cache=None):
    """Applies Transformer model on the inputs.
    Args:
      inputs: input data
      vocab_size: size of the vocabulary
      emb_dim: dimension of embedding
      num_heads: number of heads
      num_layers: number of layers
      qkv_dim: dimension of the query/key/value
      mlp_dim: dimension of the mlp on top of attention block
      max_len: maximum length.
      train: bool: if model is training.
      shift: bool: if we right-shift input - this is only disabled for
        fast, looped single-token autoregressive decoding.
      dropout_rate: dropout rate
      attention_dropout_rate: dropout rate for attention weights
      cache: flax autoregressive cache for fast decoding.
    Returns:
      output of a transformer decoder.
    """
    padding_mask = jnp.where(inputs > 0, 1, 0).astype(jnp.float32)[..., None]
    assert inputs.ndim == 2  # (batch, len)
    x = inputs
    if shift:
      x = shift_right(x)
    x = x.astype('int32')

    x = Embed(x, num_embeddings=vocab_size, features=emb_dim, name='embed')

    x = AddPositionEmbs(
        x, max_len=max_len, posemb_init=sinusoidal_init(max_len=max_len),
        cache=cache)

    x = nn.dropout(x, rate=dropout_rate, deterministic=not train)

    for _ in range(num_layers):
      x = Transformer1DBlock(
          x,
          qkv_dim=qkv_dim,
          mlp_dim=mlp_dim,
          num_heads=num_heads,
          causal_mask=True,
          padding_mask=padding_mask,
          dropout_rate=dropout_rate,
          attention_dropout_rate=attention_dropout_rate,
          deterministic=not train,
          cache=cache,
      )

    x = nn.LayerNorm(x)

    x = nn.Dense(
        x,
        rep_size,
        kernel_init=nn.initializers.xavier_uniform(),
        bias_init=nn.initializers.normal(stddev=1e-6))
    
    x = nn.relu(x)
    
    rep = jnp.max(x, axis=1)

    return rep



"""## Test models"""

@functools.partial(jax.jit, static_argnums=(1, 2))
def create_language_model(key, input_shape, model_kwargs):
  """
  We create a model definition from the top-level Language Model and 
  passed in hyperparameters.
  """
  module = TransformerLM.partial(**model_kwargs)
  # We initialize an autoregressive Cache collection for fast, autoregressive
  # decoding through the language model's decoder layers.
  with nn.attention.Cache().mutate() as cache_def:
    # create_by_shape initializes the model parameters.
    _, model = module.create_by_shape(key,
                                         [(input_shape, jnp.float32)],
                                         cache=cache_def)
  return model, cache_def


@functools.partial(jax.jit, static_argnums=(1, 2))
def create_meanpool_model(key, input_shape, model_kwargs):
  """
  We create a model definition from the top-level Representation Model and 
  passed in hyperparameters.
  """
  module = TransformerMeanPool.partial(**model_kwargs)
  # We initialize an autoregressive Cache collection for fast, autoregressive
  # decoding through the language model's decoder layers.
  with nn.attention.Cache().mutate() as cache_def:
    # create_by_shape initializes the model parameters.
    _, model = module.create_by_shape(key,
                                         [(input_shape, jnp.float32)],
                                         cache=cache_def)
  return model, cache_def


@functools.partial(jax.jit, static_argnums=(1, 2))
def create_maxpool_model(key, input_shape, model_kwargs):
  """
  We create a model definition from the top-level Representation Model and 
  passed in hyperparameters.
  """
  module = TransformerMaxPool.partial(**model_kwargs)
  # We initialize an autoregressive Cache collection for fast, autoregressive
  # decoding through the language model's decoder layers.
  with nn.attention.Cache().mutate() as cache_def:
    # create_by_shape initializes the model parameters.
    _, model = module.create_by_shape(key,
                                         [(input_shape, jnp.float32)],
                                         cache=cache_def)
  return model, cache_def


@functools.partial(jax.jit, static_argnums=(1, 2))
def create_linearmaxpool_model(key, input_shape, model_kwargs):
  """
  We create a model definition from the top-level Representation Model and 
  passed in hyperparameters.
  """
  module = TransformerLinearMaxPool.partial(**model_kwargs)
  # We initialize an autoregressive Cache collection for fast, autoregressive
  # decoding through the language model's decoder layers.
  with nn.attention.Cache().mutate() as cache_def:
    # create_by_shape initializes the model parameters.
    _, model = module.create_by_shape(key,
                                         [(input_shape, jnp.float32)],
                                         cache=cache_def)
  return model, cache_def

vocab_size = 20

input_shape = (batch_size, max_target_length)

transformer_kwargs = {
    'vocab_size': vocab_size,
    'emb_dim': lm_emb_dim,
    'num_heads': lm_num_heads,
    'num_layers': lm_num_layers,
    'qkv_dim': lm_qkv_dim,
    'mlp_dim': lm_mlp_dim,
    'max_len': max(max_target_length, max_eval_target_length)
}

transformer_linear_kwargs = {
    'vocab_size': vocab_size,
    'rep_size' : rep_size,
    'emb_dim': lm_emb_dim,
    'num_heads': lm_num_heads,
    'num_layers': lm_num_layers,
    'qkv_dim': lm_qkv_dim,
    'mlp_dim': lm_mlp_dim,
    'max_len': max(max_target_length, max_eval_target_length)
}

# generate a random sequence
random_seq_len = 32
random_seq = jnp.array([[np.random.randint(vocab_size) for _ in range(random_seq_len)] for __ in range(batch_size)])

# language model
language_model, cache_def = create_language_model(init_rng, input_shape, transformer_kwargs)
logits = language_model(random_seq)
logits, logits.shape

# mean pool representation
meanpool_model, cache_def = create_meanpool_model(init_rng, input_shape, transformer_kwargs)
meanpool_rep = meanpool_model(random_seq)
meanpool_rep, meanpool_rep.shape

# max pool representation
maxpool_model, cache_def = create_maxpool_model(init_rng, input_shape, transformer_kwargs)
maxpool_rep = maxpool_model(random_seq)
maxpool_rep, maxpool_rep.shape

# linear + ReLU + max pool representation
linearmaxpool_model, cache_def = create_linearmaxpool_model(init_rng, input_shape, transformer_linear_kwargs)
linearmaxpool_rep = linearmaxpool_model(random_seq)
linearmaxpool_rep, linearmaxpool_rep.shape

