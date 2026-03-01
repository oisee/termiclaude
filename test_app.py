#!/usr/bin/env python3
"""Test interactive app to validate termiclaude's auto-response."""

import time
import sys


def main():
    print("=== Test App ===")
    print("Initializing...")
    time.sleep(1)

    # Test 1: Y/n prompt
    r = input("\nCreate database tables? [Y/n]: ")
    print(f"  -> got: {r!r}")
    print("Creating tables... done.")
    time.sleep(0.5)

    # Test 2: Press enter
    input("\nPress ENTER to continue...")
    print("  -> continued")
    time.sleep(0.5)

    # Test 3: yes/no
    r = input("\nProceed with migration? (yes/no): ")
    print(f"  -> got: {r!r}")
    time.sleep(0.5)

    # Test 4: Numbered choice
    print("\nSelect mode:")
    print("  1. Fast")
    print("  2. Normal")
    print("  3. Thorough")
    r = input("Enter your choice: ")
    print(f"  -> got: {r!r}")
    time.sleep(0.5)

    # Test 5: confirm?
    r = input("\nDeploy to staging? confirm: ")
    print(f"  -> got: {r!r}")

    print("\n=== All prompts answered. Test complete! ===")
    return 0


if __name__ == '__main__':
    sys.exit(main())
