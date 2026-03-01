#!/usr/bin/env python3
"""Simulates an agent that goes off-rails after a prompt."""
import time, sys

print("Starting work on: fix login 401 bug")
time.sleep(1)
print("Reading auth module...")
time.sleep(2)
r = input("Modify auth.py? [Y/n]: ")
print(f"  -> {r!r}")
time.sleep(1)
print("Fixing token validation...")
time.sleep(2)
# Now go off-rails
print("\nHmm, while I'm here let me also clean up the codebase...")
time.sleep(1)
print("Running prettier on 47 files...")
time.sleep(3)
print("Updating eslint config...")
time.sleep(3)
print("Reformatting tsconfig.json...")
time.sleep(3)
r = input("\nContinue with more formatting? [Y/n]: ")
print(f"  -> {r!r}")
print("Done.")
