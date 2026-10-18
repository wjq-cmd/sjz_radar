"""Package-map prefix only; ordinary delta bunches have no NewActor header."""
import numpy as np
import sna


def exports_prefix(x):
    if not (x.hx or x.hm):
        return [], 0
    bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                         bitorder="little")[:x.pbits]
    exports = []
    pos = 0
    frags = getattr(x, "frags", None)
    if x.hx:
        if frags:
            for start, hx, _ in frags:
                if hx:
                    _, pos = sna.read_exports2(bits, start, exports)
        else:
            _, pos = sna.read_exports2(bits, pos, exports)
    if x.hm:
        start = next((s for s, _, hm in (frags or []) if hm), pos)
        n = sna.rbits(bits, start, 16)
        if n > 256:
            raise sna.Bad("too many must-map GUIDs")
        pos = start + 16
        for _ in range(n):
            _, pos = sna.packed(bits, pos)
    return exports, pos
