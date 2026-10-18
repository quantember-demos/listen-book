"""Append stdin to a file at the rate pw-cat would play it (24 kHz, 2 ch, f32)."""
import sys
import time

BYTES_PER_SECOND = 24000 * 2 * 4
with open(sys.argv[1], "ab") as out:
    while True:
        chunk = sys.stdin.buffer.read1(8192)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        time.sleep(len(chunk) / BYTES_PER_SECOND)
