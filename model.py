import collections
import argparse
import random
import cPickle
import logging
import progressbar
import os
import dynet as dy
import numpy as np

import utils

Instance = collections.namedtuple("Instance", ["sentence", "tags"])


class BiLSTM_CRF:

    def __init__(self, vocab_size, tagset_size, num_lstm_layers, embeddings, hidden_dim):
        self.model       = dy.Model()
        self.tagset_size = tagset_size
        embedding_dim    = embeddings.shape[1]

        # Word embedding parameters
        self.words_lookup = self.model.add_lookup_parameters((vocab_size, embedding_dim))
        self.words_lookup.init_from_array(embeddings)

        # LSTM parameters
        self.bi_lstm = dy.BiRNNBuilder(num_lstm_layers, embedding_dim, hidden_dim, self.model, dy.LSTMBuilder)
        
        # Matrix that maps from Bi-LSTM output to num tags
        self.lstm_to_tags_params = self.model.add_parameters((tagset_size, hidden_dim))

        # Transition matrix for tagging layer, [i,j] is score of transitioning to i from j
        self.transitions = self.model.add_lookup_parameters((tagset_size, tagset_size))


    def build_tagging_graph(self, sentence):
        dy.renew_cg()

        embeddings = [self.words_lookup[w] for w in sentence]
        lstm_out   = self.bi_lstm.transduce(embeddings)

        H = dy.parameter(self.lstm_to_tags_params)
        probs = []
        for rep in lstm_out:
            prob_t = dy.log_softmax(H * rep)
            probs.append(prob_t)

        return probs


    def score_sentence(self, observations, tags):
        assert len(observations) == len(tags)
        init_vvars = [-1e10] * self.tagset_size
        init_vvars[-2] = 0 # <Start> has all the probability
        score      = dy.scalarInput(0)
        tags = [self.tagset_size - 2] + tags
        for i, obs in enumerate(observations):
            score = score + dy.pick(self.transitions[tags[i+1]], tags[i]) + obs[tags[i+1]]
        score = score + dy.pick(self.transitions[self.tagset_size - 1], tags[-1])
        return score


    def viterbi_loss(self, sentence, tags):
        observations = self.build_tagging_graph(sentence)
        viterbi_tags, viterbi_score = self.viterbi_decoding(observations)
        if viterbi_tags != tags:
            gold_score = self.score_sentence(observations, tags)
            return viterbi_score - gold_score, viterbi_tags
        else:
            return dy.scalarInput(0), viterbi_tags


    def neg_log_loss(self, sentence, tags):
        observations  = self.build_tagging_graph(sentence)
        forward_score = self.forward(observations)
        gold_score    = self.score_sentence(observations, tags)
        return -(gold_score - forward_score)


    def forward(self, observations):

        def log_sum_exp(scores):
            # I implemented it like in this implementation
            # https://github.com/glample/tagger/blob/master/nn.py
            scores_np            = scores.npvalue()
            max_score            = np.max(scores_np, axis=0)
            max_score_broadcast  = max_score.repeat(self.tagset_size).reshape(self.tagset_size, self.tagset_size).T
            max_score_expr       = dy.inputVector(max_score)
            max_score_bcast_expr = dy.inputMatrix(max_score_broadcast.flatten(), (self.tagset_size, self.tagset_size))
            return max_score_expr + dy.log(dy.sum_cols(dy.transpose(dy.exp(scores - max_score_bcast_expr))))

        init_alphas     = [-1e10] * self.tagset_size
        init_alphas[-2] = 0
        for_expr        = dy.inputVector(init_alphas)
        trans_matrix    = dy.concatenate_cols([self.transitions[idx] for idx in xrange(self.tagset_size)])
        for i, obs in enumerate(observations):
            obs_matrix  = dy.transpose(dy.concatenate_cols([obs] * self.tagset_size))
            prev_matrix = dy.concatenate_cols([for_expr] * self.tagset_size)
            scores      = obs_matrix + prev_matrix + trans_matrix
            for_expr    = log_sum_exp(scores)
        terminal_expr  = for_expr + self.transitions[self.tagset_size - 1]
        terminal_np    = terminal_expr.npvalue()
        terminal_max   = np.max(terminal_np)
        max_expr       = dy.scalarInput(terminal_max)
        max_bcast      = terminal_max.repeat(self.tagset_size).reshape(self.tagset_size)
        max_bcast_expr = dy.inputVector(max_bcast)
        alpha         = max_expr + dy.log(dy.sum_cols(dy.transpose(dy.exp(terminal_expr - max_bcast_expr))))
        return alpha


    def viterbi_decoding(self, observations):
        backpointers = []
        init_vvars   = [-1e10] * self.tagset_size
        init_vvars[-2] = 0 # <Start> has all the probability
        for_expr     = dy.inputVector(init_vvars)
        trans_exprs  = [self.transitions[idx] for idx in range(self.tagset_size)]
        for i, obs in enumerate(observations):
            bptrs_t = []
            vvars_t = []
            for next_tag in range(self.tagset_size):
                next_tag_expr = for_expr + trans_exprs[next_tag]
                next_tag_arr = next_tag_expr.npvalue()
                best_tag_id  = np.argmax(next_tag_arr)
                bptrs_t.append(best_tag_id)
                vvars_t.append(dy.pick(next_tag_expr, best_tag_id))
            for_expr = dy.concatenate(vvars_t) + obs
            backpointers.append(bptrs_t)
        # Perform final transition to terminal
        terminal_expr = for_expr + trans_exprs[-1]
        terminal_arr  = terminal_expr.npvalue()
        best_tag_id   = np.argmax(terminal_arr)
        path_score    = dy.pick(terminal_expr, best_tag_id)
        # Reverse over the backpointers to get the best path
        best_path = [best_tag_id] # Start with the tag that was best for terminal
        for bptrs_t in reversed(backpointers):
            best_tag_id = bptrs_t[best_tag_id]
            best_path.append(best_tag_id)
        best_path.pop() # Remove the start symbol
        # Return best path and best path's score
        return best_path, path_score

    @property
    def model(self):
        return self.model


# ===-----------------------------------------------------------------------===
# Argument parsing
# ===-----------------------------------------------------------------------===
parser = argparse.ArgumentParser()
parser.add_argument("--dataset", required=True, dest="dataset", help=".pkl file to use")
parser.add_argument("--embeddings", required=True, dest="embeddings", help="File from which to read in pretrained embeds")
parser.add_argument("--num-epochs", default=15, dest="num_epochs", help="Number of full passes through training set")
parser.add_argument("--lstm-layers", default=2, dest="lstm_layers", help="Number of LSTM layers")
parser.add_argument("--hidden-dim", default=128, dest="hidden_dim", help="Size of LSTM hidden layers")
parser.add_argument("--learning-rate", default=0.001, dest="learning_rate", help="Initial learning rate")
parser.add_argument("--dropout", default=-1, dest="dropout", help="Amount of dropout to apply to LSTM part of graph")
parser.add_argument("--log-dir", default="log", dest="log_dir", help="Directory where to write logs / serialized models")
options = parser.parse_args()


# ===-----------------------------------------------------------------------===
# Set up logging
# ===-----------------------------------------------------------------------===
if not os.path.exists(options.log_dir):
    os.mkdir(options.log_dir)
logging.basicConfig(filename=options.log_dir + "/log.txt", filemode="w", format="%(message)s", level=logging.INFO)
train_dev_cost = utils.CSVLogger(options.log_dir + "/train_dev.log", ["Train.cost", "Dev.cost"])


# ===-----------------------------------------------------------------------===
# Log some stuff about this run
# ===-----------------------------------------------------------------------===


# ===-----------------------------------------------------------------------===
# Read in dataset
# ===-----------------------------------------------------------------------===
dataset = cPickle.load(open(options.dataset, "r"))
w2i = dataset["w2i"]
t2i = dataset["t2i"]
i2w = { i: w for w, i in w2i.items() } # Inverse mapping
i2t = { i: t for t, i in t2i.items() }
tag_list = [ i2t[idx] for idx in xrange(len(i2t)) ] # To use in the confusion matrix
training_instances = dataset["training_instances"]
dev_instances      = dataset["dev_instances"]
test_instances     = dataset["test_instances"]


# ===-----------------------------------------------------------------------===
# Build model and trainer
# ===-----------------------------------------------------------------------===
embeddings = utils.read_pretrained_embeddings(options.embeddings, w2i)
bilstm_crf = BiLSTM_CRF(len(w2i), len(t2i), options.lstm_layers, embeddings, options.hidden_dim)
trainer    = dy.AdamTrainer(bilstm_crf.model, options.learning_rate)

for epoch in xrange(options.num_epochs):
    bar = progressbar.ProgressBar()
    random.shuffle(training_instances)
    train_loss = 0.0
    for instance in bar(training_instances):
        loss_expr   = bilstm_crf.neg_log_loss(instance.sentence, instance.tags)
        loss        = loss_expr.scalar_value()
        train_loss += (loss / len(instance.sentence))
        loss_expr.backward()
        trainer.update()
    logging.info("Epoch {} complete".format(epoch + 1))

    # Evaluate dev data
    logging.info("Evaluating on dev data...")
    bar = progressbar.ProgressBar()
    cm = utils.ConfusionMatrix(tag_list)
    dev_loss = 0.0
    random.shuffle(dev_instances)
    all_gold    = []
    all_viterbi = []
    for instance in bar(dev_instances):
        viterbi_loss, viterbi_tags = bilstm_crf.viterbi_loss(instance.sentence, instance.tags)
        dev_loss    += (viterbi_loss.value() / len(instance.sentence))
        gold_tags    = [ i2t[t] for t in instance.tags ]
        viterbi_tags = [ i2t[t] for t in viterbi_tags ]
        all_gold    += gold_tags
        all_viterbi += viterbi_tags
    correct_tags = 0
    for gold, viterbi in zip(all_gold, all_viterbi):
        if gold == viterbi:
            correct_tags += 1
    logging.info("Accuracy: {}".format(float(correct_tags) / len(all_gold)))
    cm.add(all_gold, all_viterbi)
    train_loss = train_loss / len(training_instances)
    dev_loss   = dev_loss / len(dev_instances)
    train_dev_cost.add_column([str(train_loss), str(dev_loss)])
cm.plot()
