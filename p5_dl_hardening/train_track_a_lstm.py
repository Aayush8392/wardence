"""
Trains the real DeepLog LSTM per Tier-1 service (front-end/orders/user),
on the real sliding-window sequences from track_a_deeplog_sequences.py.

Deliberately small model -- matches this project's already-locked
"shallow, not deep" Phase G design principle (see wardence_context.md's
"Real trained multi-class fault classifier" entry): a tiny embedding +
single-layer LSTM, sized against our real, small vocab (7-39 templates),
not a heavy architecture that would be overfit theater at this scale.

Real early stopping on validation loss (patience-based, not a fixed
epoch count) -- the test set is touched exactly once, after training
decisions are already locked in, never used to pick hyperparameters.

Reports both cross-entropy loss and top-K accuracy (K=1,3,5) -- DeepLog's
real anomaly criterion at inference time is "is the actual next
template within the model's top-K predicted candidates," not just
top-1 exact match, so top-K accuracy is the more meaningful real metric
here, not just an extra.

Requires: pip install torch

Usage:
    python3 train_track_a_lstm.py
"""

import os

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from deeplog_service_config import DEEPLOG_TIER_1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_A_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_a")

EMBEDDING_DIM = 16
HIDDEN_SIZE = 32
BATCH_SIZE = 256
MAX_EPOCHS = 100
PATIENCE = 5  # real early stopping: stop after this many epochs with no real val-loss improvement
LEARNING_RATE = 1e-3
TOP_K_VALUES = [1, 3, 5]

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class DeepLogLSTM(nn.Module):
    def __init__(self, vocab_size: int, embedding_dim: int, hidden_size: int):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        self.lstm = nn.LSTM(embedding_dim, hidden_size, num_layers=1, batch_first=True)
        self.output = nn.Linear(hidden_size, vocab_size)

    def forward(self, x):
        emb = self.embedding(x)
        _, (h_n, _) = self.lstm(emb)
        return self.output(h_n[-1])  # logits over the real vocab


def load_data(service: str):
    path = os.path.join(TRACK_A_DIR, f"{service}_sequences.npz")
    data = np.load(path)
    vocab = list(data["vocab"])
    id_to_index = {tid: i for i, tid in enumerate(vocab)}

    def remap(X, y):
        # Real drain3 cluster IDs -> 0-indexed contiguous class indices,
        # same necessary remapping as Track B's HMM (nn.Embedding/
        # CrossEntropyLoss both need contiguous 0..N-1 indices).
        X_idx = np.vectorize(id_to_index.get)(X)
        y_idx = np.vectorize(id_to_index.get)(y)
        return X_idx.astype(np.int64), y_idx.astype(np.int64)

    X_train, y_train = remap(data["X_train"], data["y_train"])
    X_val, y_val = remap(data["X_val"], data["y_val"])
    X_test, y_test = remap(data["X_test"], data["y_test"])
    return len(vocab), (X_train, y_train), (X_val, y_val), (X_test, y_test)


def make_loader(X, y, shuffle: bool):
    ds = TensorDataset(torch.from_numpy(X), torch.from_numpy(y))
    return DataLoader(ds, batch_size=BATCH_SIZE, shuffle=shuffle)


def run_epoch(model, loader, criterion, optimizer=None):
    is_train = optimizer is not None
    model.train() if is_train else model.eval()
    total_loss, total_count = 0.0, 0

    with torch.set_grad_enabled(is_train):
        for X_batch, y_batch in loader:
            X_batch, y_batch = X_batch.to(DEVICE), y_batch.to(DEVICE)
            logits = model(X_batch)
            loss = criterion(logits, y_batch)

            if is_train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            total_loss += loss.item() * len(y_batch)
            total_count += len(y_batch)

    return total_loss / total_count


def top_k_accuracy(model, loader, k_values: list):
    model.eval()
    correct = {k: 0 for k in k_values}
    total = 0
    with torch.no_grad():
        for X_batch, y_batch in loader:
            X_batch, y_batch = X_batch.to(DEVICE), y_batch.to(DEVICE)
            logits = model(X_batch)
            max_k = max(k_values)
            top_preds = logits.topk(min(max_k, logits.shape[1]), dim=1).indices
            for k in k_values:
                hit = (top_preds[:, :k] == y_batch.unsqueeze(1)).any(dim=1)
                correct[k] += hit.sum().item()
            total += len(y_batch)
    return {k: correct[k] / total for k in k_values}


def train_service(service: str):
    print(f"[{service}]")
    vocab_size, (X_train, y_train), (X_val, y_val), (X_test, y_test) = load_data(service)
    print(f"  vocab_size={vocab_size}, train={len(X_train)}, val={len(X_val)}, test={len(X_test)}")

    train_loader = make_loader(X_train, y_train, shuffle=True)  # shuffling WITHIN train batches is
    val_loader = make_loader(X_val, y_val, shuffle=False)       # fine -- the chronological split
    test_loader = make_loader(X_test, y_test, shuffle=False)    # itself was never shuffled

    model = DeepLogLSTM(vocab_size, EMBEDDING_DIM, HIDDEN_SIZE).to(DEVICE)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

    best_val_loss = float("inf")
    best_state = None
    epochs_without_improvement = 0

    for epoch in range(1, MAX_EPOCHS + 1):
        train_loss = run_epoch(model, train_loader, criterion, optimizer)
        val_loss = run_epoch(model, val_loader, criterion)

        improved = val_loss < best_val_loss - 1e-5
        if improved:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1

        print(f"  epoch {epoch:>3}: train_loss={train_loss:.4f} val_loss={val_loss:.4f}"
              f"{'  (new best)' if improved else ''}")

        if epochs_without_improvement >= PATIENCE:
            print(f"  early stop -- no real val improvement for {PATIENCE} epochs")
            break

    model.load_state_dict(best_state)  # real best-val checkpoint, not the last epoch

    test_loss = run_epoch(model, test_loader, criterion)
    test_topk = top_k_accuracy(model, test_loader, TOP_K_VALUES)
    print(f"  real held-out TEST loss: {test_loss:.4f}")
    print(f"  real held-out TEST top-K accuracy: " +
          ", ".join(f"top-{k}={acc * 100:.1f}%" for k, acc in test_topk.items()))

    model_path = os.path.join(TRACK_A_DIR, f"{service}_lstm.pt")
    torch.save({
        "state_dict": best_state,
        "vocab_size": vocab_size,
        "embedding_dim": EMBEDDING_DIM,
        "hidden_size": HIDDEN_SIZE,
    }, model_path)
    print(f"  wrote {model_path}")
    print()


def main():
    print(f"Training on device: {DEVICE}\n")
    for service in DEEPLOG_TIER_1:
        train_service(service)


if __name__ == "__main__":
    main()
