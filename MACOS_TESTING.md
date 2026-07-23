# macOS Messages Integration Testing

This guide explains how to run macOS Messages integration tests locally on an iMac.

## Overview

By default, tests that interact with macOS Messages.app via AppleScript are **skipped** both in CI and local development. They require:
- macOS (Darwin platform)
- Messages.app (Apple's default iMessage client)
- `osascript` command-line tool

These tests are opt-in to keep CI fast and to prevent failures in non-macOS environments.

## Running macOS Integration Tests Locally

### Option 1: Run Only macOS Integration Tests

```bash
export PYTEST_MACOS_INTEGRATION=1
pytest -v -m macos_integration
```

This runs **only** the tests marked with `@pytest.mark.macos_integration`.

### Option 2: Run All Tests (Including macOS Integration)

```bash
export PYTEST_MACOS_INTEGRATION=1
pytest -v
```

This runs the entire test suite, including macOS integration tests.

### Option 3: Run a Specific macOS Integration Test

```bash
export PYTEST_MACOS_INTEGRATION=1
pytest -v tests/test_ivy_core.py::test_argv_round_trip_with_tricky_characters_real_osascript
```

## Local Smoke Tests (iMac Only)

For manual testing of the actual iMessage sending functionality, follow these steps:

### Smoke Test 1: Send a Text Message

```bash
python3 << 'EOF'
import sys
sys.path.insert(0, '/path/to/Ivys-Base')

from ivy_core.messaging import send_imessage

# Replace with a real phone number on your iMessage contact list
result = send_imessage("+1-XXX-XXX-XXXX", "Test message from automated script")
print(f"Result: {result}")
EOF
```

Expected: Messages.app should receive the message in the specified conversation thread.

### Smoke Test 2: Send a PDF Attachment

```bash
python3 << 'EOF'
import sys
sys.path.insert(0, '/path/to/Ivys-Base')

from ivy_core.messaging import send_imessage_attachment
from pathlib import Path

# Create a minimal PDF for testing
pdf_path = Path("/tmp/test_attachment.pdf")
pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\nxref\ntrailer\n<</Size 2 /Root 1 0 R>>\n%%EOF")

# Replace with a real phone number on your iMessage contact list
receipt = send_imessage_attachment("+1-XXX-XXX-XXXX", str(pdf_path), caption="Test PDF")
print(f"Receipt status: {receipt.status}")
print(f"Delivery: {'✓' if receipt else '✗'}")
EOF
```

Expected: Messages.app should receive the PDF in the specified conversation thread.

### Smoke Test 3: Run the Integration Test Suite

```bash
export PYTEST_MACOS_INTEGRATION=1
pytest -v -m macos_integration
```

This runs all tests marked as `macos_integration`, which currently includes:
- `test_argv_round_trip_with_tricky_characters_real_osascript` — verifies AppleScript argv escaping with real `osascript`

## Notes

- **CI Always Skips These Tests**: The GitHub Actions CI runner sets `PYTEST_MACOS_INTEGRATION=0` (or unset), so macOS integration tests are never run in CI.
- **Mocking in Tests**: Most unit tests mock the `AppleScriptRunner` via `unittest.mock.patch`, so they work everywhere.
- **Security**: AppleScript arguments are passed via process argv, not interpolated into source code, preventing string-literal injection attacks.
- **Staging Directory**: Attachments are staged in `~/Pictures/.ivy_outbound/` because Messages.app's sandbox rejects files from most home directories (verified 2026-06-29).

## Troubleshooting

### "osascript: command not found"

You're not on macOS, or `osascript` is not in your PATH. These tests require macOS.

### "Message not received"

Check that:
1. The phone number is in your iMessage contacts
2. Messages.app is not in Do Not Disturb mode
3. The iMessage service is available and signed in
4. The recipient is also using iMessage

### Attachment Fails with "FILE_MISSING"

Ensure the PDF file path is absolute and the file exists and is non-empty.

## CI Behavior

The GitHub Actions workflow explicitly sets `PYTEST_MACOS_INTEGRATION=0`, so the `pytest_configure` hook in `conftest.py` removes the `macos_integration` marker from the test run. This ensures:
- CI completes quickly
- CI doesn't require macOS infrastructure
- No false positives from mocked osascript calls
