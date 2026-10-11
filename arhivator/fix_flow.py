#!/usr/bin/env python3
"""Remove single newlines within paragraphs, preserve blank lines."""

import re

path = "flow.md"

with open(path, "r") as f:
    text = f.read()

text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)

with open(path, "w") as f:
    f.write(text)

print("Done.")
