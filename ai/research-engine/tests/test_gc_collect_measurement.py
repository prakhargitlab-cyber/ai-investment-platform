"""Focused measurement: does explicit gc.collect() after PDF extraction
actually improve memory retention vs CPython's default GC behavior?

Uses pypdf to generate synthetic PDFs programmatically.
"""
from __future__ import annotations

import gc
import time
from io import BytesIO

import pytest


def _get_rss_kb():
    """Get current RSS in KB. Returns None if /proc unavailable."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, OSError):
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        return None


def _make_valid_pdf(num_pages=50):
    """Create a valid PDF using pypdf's PdfWriter with simple blank pages."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for i in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pypdf_gc_collect_necessity():
    """Determine whether explicit gc.collect() is needed after pypdf extraction.

    Key questions:
    1. After del reader + del extracted_pages, does gc.collect() find unreachable
       cyclic garbage? (i.e., are pypdf's objects cyclic?)
    2. Does CPython's automatic GC already reclaim cyclic objects during
       repeated extraction (without explicit gc.collect)?
    3. What is the latency cost of gc.collect()?
    """
    from pypdf import PdfReader

    pdf_content = _make_valid_pdf(num_pages=50)
    print(f"Generated synthetic PDF: {len(pdf_content)} bytes, 50 pages")

    # Critical test: after del reader (without gc.collect), does gc.collect() find cyclic garbage?
    gc.collect()
    reader = PdfReader(BytesIO(pdf_content))
    _ = [page.extract_text() for page in reader.pages]
    del reader
    collected = gc.collect()
    print(f"\n=== Cyclic garbage detection ===")
    print(f"After del reader (no gc.collect), gc.collect() found: {collected} objects")

    # Now test: without explicit gc.collect, does auto-GC kick in during repeated extraction?
    gc.collect()
    counts_start = gc.get_count()
    print(f"gc counts at start: {counts_start}")
    rss_before = _get_rss_kb()

    for i in range(10):
        reader = PdfReader(BytesIO(pdf_content))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages_text)
        del reader, pages_text, text

    counts_end = gc.get_count()
    rss_after_no_gc = _get_rss_kb()
    auto_collected = gc.collect()
    print(f"gc counts after 10 extractions (no gc.collect): {counts_end}")
    print(f"RSS before: {rss_before}, RSS after (no gc): {rss_after_no_gc}")
    print(f"gc.collect() after auto-GC cycle found: {auto_collected} objects")

    # Did auto-GC trigger? If gen0 count went from high back to near 0, it did.
    auto_gc_triggered = counts_end[0] < counts_start[0]
    auto_gc_was_sufficient = auto_collected == 0

    # Latency measurement
    start = time.perf_counter()
    for i in range(5):
        reader = PdfReader(BytesIO(pdf_content))
        pages_text = [page.extract_text() for page in reader.pages]
        text = "\n".join(pages_text)
        del reader, pages_text, text
    elapsed_no_gc = time.perf_counter() - start

    start = time.perf_counter()
    for i in range(5):
        reader = PdfReader(BytesIO(pdf_content))
        pages_text = [page.extract_text() for page in reader.pages]
        text = "\n".join(pages_text)
        del reader
        gc.collect()
        del pages_text, text
    elapsed_with_gc = time.perf_counter() - start

    per_extract_no_gc = elapsed_no_gc / 5 * 1000
    per_extract_with_gc = elapsed_with_gc / 5 * 1000
    gc_overhead = (elapsed_with_gc - elapsed_no_gc) / 5 * 1000

    print(f"\n=== Latency (5 extractions, 50-page PDF) ===")
    print(f"Without gc.collect(): {per_extract_no_gc:.2f} ms/extraction")
    print(f"With gc.collect():    {per_extract_with_gc:.2f} ms/extraction")
    print(f"gc.collect() overhead: {gc_overhead:.2f} ms/extraction")
    print(f"Extraction timeout budget: 12000 ms")
    if gc_overhead > 0:
        print(f"gc.collect() overhead as % of timeout: {gc_overhead/12000*100:.4f}%")

    print(f"\n=== GC Analysis ===")
    print(f"Auto-GC triggered during extraction: {auto_gc_triggered}")
    print(f"Auto-GC was sufficient (0 cyclic left): {auto_gc_was_sufficient}")
    print(f"Cyclic objects found after del reader: {collected}")

    if not auto_gc_was_sufficient or collected > 0:
        print(f"\n=== CONCLUSION: gc.collect() IS justified ===")
        print(f"Cyclic garbage exists after del reader ({collected} objects).")
        print(f"If auto-GC is insufficient, explicit gc.collect() provides prompt reclamation.")
    else:
        print(f"\n=== CONCLUSION: gc.collect() may NOT be needed ===")
        print(f"Auto-GC already reclaims cyclic objects. Explicit gc.collect adds {gc_overhead:.2f}ms overhead.")

    # This is a measurement test -- print conclusions for human review
    # Don't assert either way; the measurement data determines the answer.


def test_gc_collect_cost_with_accumulated_state():
    """Measure gc.collect() cost in a process simulating long-running research engine."""
    from pypdf import PdfReader

    pdf_content = _make_valid_pdf(num_pages=20)

    # Simulate accumulated state (research engine processes thousands of instruments)
    _accumulated = []
    for _ in range(500000):
        _accumulated.append({"k": "v", "data": [1, 2, 3]})

    start = time.perf_counter()
    for _ in range(100):
        gc.collect()
    elapsed = time.perf_counter() - start

    per_call = elapsed / 100 * 1000
    print(f"\n=== gc.collect() cost with 500K accumulated objects ===")
    print(f"Average gc.collect() cost: {per_call:.3f} ms/call")
    print(f"Overhead per extraction: {per_call:.3f} ms")
    print(f"Extraction timeout budget: 12000 ms")
    print(f"gc.collect() overhead as % of timeout: {per_call/12000*100:.4f}%")
