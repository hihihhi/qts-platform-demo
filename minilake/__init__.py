"""minilake: an independent demonstration system written only from the platform's public write-up; not
the platform's code.

A small stand-in, standard library only, that shows the write-up's ideas end to end on SYNTHETIC
data. It shares no code with the platform the write-up describes. The write-up:
https://github.com/hihihhi/qts-platform-showcase

    synth     a SYNTHETIC tick delivery with planted problems
    raw       layer 1: delivered files kept byte for byte, under a SHA-256 manifest
    table     versioned, typed, column-oriented tables with atomic publish and pinned reads
    pipeline  layer 2 (typed) and layer 3 (cleansed) built from the raw layer
    cleanse   the cleansing rules, their per-day counts and correction log, and the layer diff
    query     fetch(): pinned snapshots, partition pruning, bars, and the as-of guard
    contend   concurrent reader, writer and rewriter processes on one table
    gates     every invariant as a check; exit 0 only if all hold
"""
