"""In-memory ledger, no persistence."""


class Ledger:
    def __init__(self):
        self.entries = []

    def add(self, amount, note=""):
        self.entries.append({"amount": amount, "note": note})
        return len(self.entries)

    def total(self):
        return sum(entry["amount"] for entry in self.entries)
