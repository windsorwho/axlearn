# Copyright © 2023 Apple Inc.

"""Tests autoregressive models."""
import jax.random
import numpy as np
from absl.testing import absltest
from jax import numpy as jnp
from transformers.models.gpt2 import modeling_gpt2 as hf_gpt2

from axlearn.common import causal_lm, utils
from axlearn.common.attention import StackedTransformerLayer
from axlearn.common.loss import cross_entropy
from axlearn.common.metrics import MetricAccumulator
from axlearn.common.module import (
    InvocationContext,
    functional,
    new_output_collection,
    set_current_context,
)
from axlearn.common.param_converter import as_torch_tensor
from axlearn.common.param_init import PARAM_REGEXP_WEIGHT, DefaultInitializer, WeightInitializer
from axlearn.common.test_utils import TestCase, assert_allclose
from axlearn.common.torch_utils import parameters_from_torch_layer


class Gpt2TransformerTest(TestCase):
    def test_against_hf_gpt2_lm(self):
        hidden_dim = 16
        vocab_size = 24
        num_heads = 4
        num_layers = 2
        source_length = 11
        # Reference implementation.
        ref_cfg = hf_gpt2.GPT2Config(
            n_embd=hidden_dim,
            n_head=num_heads,
            n_layer=num_layers,
            n_positions=source_length,
            vocab_size=vocab_size,
            attn_pdrop=0.0,
            embd_pdrop=0.0,
            resid_pdrop=0.0,
        )
        ref_layer = hf_gpt2.GPT2LMHeadModel(ref_cfg).eval()
        # Equivalent AXLearn implementation.
        # The config has similarities with some in encoder_test.py.
        # pylint: disable=duplicate-code
        decoder_cfg = causal_lm.gpt_decoder_config(
            stack_cfg=StackedTransformerLayer.default_config(),
            num_layers=num_layers,
            hidden_dim=hidden_dim,
            num_heads=num_heads,
            vocab_size=vocab_size,
            activation_function="nn.gelu",
            max_position_embeddings=source_length,
            layer_norm_epsilon=ref_cfg.layer_norm_epsilon,
            dropout_rate=ref_cfg.attn_pdrop,
        )
        decoder_cfg.param_init = DefaultInitializer.default_config().set(
            init_by_param_name={
                PARAM_REGEXP_WEIGHT: WeightInitializer.default_config().set(
                    fan=None, scale=0.02, distribution="normal"
                )
            }
        )
        layer = (
            causal_lm.Model.default_config()
            .set(
                decoder=decoder_cfg,
                name="layer_test",
            )
            .instantiate(parent=None)
        )
        input_ids = np.random.randint(1, vocab_size, size=(3, source_length))
        (_, test_aux), ref_outputs = self._compute_layer_outputs(
            test_layer=layer,
            ref_layer=ref_layer,
            test_inputs=dict(input_batch=dict(input_ids=input_ids), return_aux=True),
            ref_inputs=as_torch_tensor(input_ids),
            parameters_from_ref_layer=parameters_from_torch_layer,
        )
        test_logits = test_aux["logits"]
        ref_logits = ref_outputs.logits.detach().numpy()
        assert_allclose(test_logits, ref_logits)


class ModelMetricsTest(TestCase):
    def test_metrics(self):
        decoder_cfg = causal_lm.gpt_decoder_config(
            stack_cfg=StackedTransformerLayer.default_config(),
            num_layers=1,
            hidden_dim=10,
            num_heads=2,
            vocab_size=10,
            activation_function="nn.gelu",
            max_position_embeddings=10,
            layer_norm_epsilon=0.1,
            dropout_rate=0.0,
        )
        model = (
            causal_lm.Model.default_config()
            .set(
                decoder=decoder_cfg,
                name="metrics_test",
            )
            .instantiate(parent=None)
        )

        prng_key, init_key = jax.random.split(jax.random.PRNGKey(123))
        model_params = model.initialize_parameters_recursively(init_key)
        # Compute summaries after forwarding two batches.
        # The second batch is a dummy one - should not affect metrics.
        target_labels = jnp.array([[[1, 3, 0], [2, 3, 1]], [[0, 0, 0], [0, 0, 0]]])
        logits = jnp.array(
            [
                [
                    [
                        [0.1, 0.9, 0.1, 0.1],  # Target 1; pred 1.
                        [0.1, 0.1, 0.9, 0.1],  # Target 3; pred 2.
                        [0.9, 0.1, 0.1, 0.1],  # Target 0; pred 0.
                    ],  # Example 0.
                    [
                        [0.1, 0.1, 0.9, 0.1],  # Target 2; pred 2.
                        [0.1, 0.1, 0.9, 0.1],  # Target 3; pred 2.
                        [0.9, 0.1, 0.1, 0.1],  # Target 1; pred 0.
                    ],  # Example 1.
                ],  # Batch 0.
                [
                    [
                        [0.1, 0.9, 0.1, 0.1],  # Target 0; pred 1.
                        [0.1, 0.1, 0.9, 0.1],  # Target 0; pred 2.
                        [0.9, 0.1, 0.1, 0.1],  # Target 0; pred 0.
                    ],  # Example 0.
                    [
                        [0.1, 0.1, 0.9, 0.1],  # Target 0; pred 2.
                        [0.1, 0.1, 0.9, 0.1],  # Target 0; pred 2.
                        [0.9, 0.1, 0.1, 0.1],  # Target 0; pred 0.
                    ],  # Example 1.
                ],  # Batch 1.
            ]
        )
        target_num_bytes = jnp.array([[3, 7], [0, 0]])
        live_targets = jnp.array([[[1, 1, 0], [1, 1, 1]], [[0, 0, 0], [0, 0, 0]]])
        accumulator = MetricAccumulator.default_config().instantiate()
        for i in range(2):
            _, output_collection = functional(
                model,
                inputs=dict(
                    logits=logits[i],
                    target_labels=target_labels[i],
                    target_num_bytes=target_num_bytes[i],
                ),
                is_training=True,
                prng_key=prng_key,
                state=model_params,
                method="_metrics",
            )
            accumulator.update(output_collection.summaries)
        summaries = accumulator.summaries()
        # Only the first batch should affect results.
        loss, loss_dict = cross_entropy(
            logits=logits[0],
            target_labels=target_labels[0],
            mask=live_targets[0],
        )
        self.assertEqual(2.0 / 5, summaries["accuracy"].mean)
        self.assertAlmostEqual(loss, summaries["loss"].mean)
        self.assertEqual(5, summaries["loss"].weight)
        self.assertAlmostEqual(jnp.exp(loss), summaries["perplexity"].mean, places=6)
        per_token_loss = loss_dict["pre_mask_loss"] * live_targets
        total_bytes = target_num_bytes.sum()
        bits_per_byte = per_token_loss.sum() / jnp.maximum(1, total_bytes) / jnp.log(2)
        self.assertAlmostEqual(bits_per_byte, summaries["bits_per_byte"].mean)

    def test_forward(self):
        batch_size, seq_len, vocab_size = 3, 10, 10

        decoder_cfg = causal_lm.gpt_decoder_config(
            stack_cfg=StackedTransformerLayer.default_config(),
            num_layers=2,
            hidden_dim=10,
            num_heads=2,
            vocab_size=vocab_size,
            activation_function="nn.gelu",
            max_position_embeddings=seq_len,
            layer_norm_epsilon=0.1,
            dropout_rate=0.0,
        )
        model_cfg = causal_lm.Model.default_config().set(decoder=decoder_cfg, name="metrics_test")
        model = model_cfg.instantiate(parent=None)

        prng_key, init_key = jax.random.split(jax.random.PRNGKey(123))
        model_params = model.initialize_parameters_recursively(init_key)

        input_ids = jax.random.randint(
            jax.random.PRNGKey(123), shape=[batch_size, seq_len], minval=0, maxval=vocab_size
        )
        target_labels = jax.random.randint(
            jax.random.PRNGKey(123), shape=[batch_size, seq_len], minval=-1, maxval=vocab_size
        )
        input_batch = dict(input_ids=input_ids, target_labels=target_labels)

        # Ensure that forward outputs are consistent with metrics output.
        ctx = InvocationContext(
            name="root",
            parent=None,
            module=model,
            state=model_params,
            output_collection=new_output_collection(),
            is_training=True,
            prng_key=prng_key,
        )
        with set_current_context(ctx):
            loss, aux = model.forward(input_batch=input_batch, return_aux=True)
            # pylint: disable-next=protected-access
            ref_outputs = model._metrics(
                logits=aux["logits"], target_labels=target_labels, target_num_bytes=None
            )
            self.assertAlmostEqual(loss, ref_outputs["loss"])
            self.assertTrue(jnp.allclose(aux["per_label_loss"], ref_outputs["per_token_loss"]))


if __name__ == "__main__":
    with utils.numeric_checks(True):
        absltest.main()
