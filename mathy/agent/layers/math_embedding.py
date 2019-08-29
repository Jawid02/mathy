import tensorflow as tf

from ...core.expressions import MathTypeKeysMax
from ...features import (
    FEATURE_BWD_VECTORS,
    FEATURE_FWD_VECTORS,
    FEATURE_LAST_BWD_VECTORS,
    FEATURE_LAST_FWD_VECTORS,
    FEATURE_LAST_RULE,
    FEATURE_MOVE_COUNTER,
    FEATURE_MOVES_REMAINING,
    FEATURE_NODE_COUNT,
    FEATURE_PROBLEM_TYPE,
)
from .lstm import LSTM
from .resnet_stack import ResNetStack


class MathEmbedding(tf.keras.layers.Layer):
    def __init__(self, units: int = 128, **kwargs):
        self.units = units
        self.flatten = tf.keras.layers.Flatten()
        self.concat = tf.keras.layers.Concatenate(name=f"embedding_concat")
        self.input_dense = tf.keras.layers.Dense(
            self.units, name="embedding_input", use_bias=False
        )
        self.resnet = ResNetStack(units=units, name="embedding_resnet", num_layers=4)
        self.lstm = LSTM(units, name="embedding_lstm")
        self.embedding = tf.keras.layers.Dense(
            units=self.units, name="embedding", activation="relu"
        )
        self.build_feature_columns()
        super(MathEmbedding, self).__init__(**kwargs)

    def build_feature_columns(self):
        """Build out the Tensorflow Feature Columns that define the inputs from Mathy
        into the neural network."""
        self.f_move_count = tf.feature_column.numeric_column(
            key=FEATURE_MOVE_COUNTER, dtype=tf.int64
        )
        self.f_moves_remaining = tf.feature_column.numeric_column(
            key=FEATURE_MOVES_REMAINING, dtype=tf.int64
        )
        self.f_last_rule = tf.feature_column.numeric_column(
            key=FEATURE_LAST_RULE, dtype=tf.int64
        )
        self.f_node_count = tf.feature_column.numeric_column(
            key=FEATURE_NODE_COUNT, dtype=tf.int64
        )
        self.f_problem_type = tf.feature_column.indicator_column(
            tf.feature_column.categorical_column_with_identity(
                key=FEATURE_PROBLEM_TYPE, num_buckets=32
            )
        )
        self.feature_columns = [
            self.f_problem_type,
            self.f_last_rule,
            self.f_node_count,
            self.f_move_count,
            # self.f_moves_remaining,
        ]

        self.ctx_extractor = tf.keras.layers.DenseFeatures(
            self.feature_columns, name="ctx_features"
        )

    def call(self, features):
        context_inputs = self.ctx_extractor(features)
        sequence_length = len(features[FEATURE_BWD_VECTORS][0])
        max_depth = MathTypeKeysMax
        batch_seq_one_hots = [
            tf.one_hot(
                features[FEATURE_BWD_VECTORS], dtype=tf.float32, depth=max_depth
            ),
            tf.one_hot(
                features[FEATURE_FWD_VECTORS], dtype=tf.float32, depth=max_depth
            ),
            tf.one_hot(
                features[FEATURE_LAST_BWD_VECTORS], dtype=tf.float32, depth=max_depth
            ),
            tf.one_hot(
                features[FEATURE_LAST_FWD_VECTORS], dtype=tf.float32, depth=max_depth
            ),
        ]
        batch_sequence_features = self.concat(batch_seq_one_hots)

        outputs = []
        # Convert one-hot to dense layer representations
        for sequence_inputs in batch_sequence_features:
            example = self.resnet(self.input_dense(self.flatten(sequence_inputs)))
            outputs.append(tf.reshape(example, [sequence_length, -1]))

        outputs = tf.convert_to_tensor(outputs)

        hidden_states, lstm_vectors = self.lstm(outputs)
        return (context_inputs, lstm_vectors, hidden_states, sequence_length)
