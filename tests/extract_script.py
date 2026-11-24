"""Print the page's inline script, so `node --check` can confirm it parses."""
import re
import sys

html = open(sys.argv[1], encoding="utf-8").read()
blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
if not blocks:
    sys.exit("no inline script found")
print("\n;\n".join(blocks))
