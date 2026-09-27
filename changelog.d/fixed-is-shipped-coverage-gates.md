Fixed `is_shipped` false positives on four question shapes: answers now require
the question's rarest distinctive terms to appear in the evidence commit, each
clause of multi-part questions ("X that also does Y") must be covered, "X
instead of Y" no longer matches commits that shipped "Y instead of X", and a
question naming a repo falls back to grepping that repo's pre-window git
history (cached) when the index has no qualifying commit.
