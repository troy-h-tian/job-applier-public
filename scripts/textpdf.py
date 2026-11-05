"""Write plain text to a simple PDF (Letter, Helvetica 11 pt), for forms that want a cover letter file.
No external tools: the PDF is assembled by hand."""

import textwrap


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf(path, text, size=11, lead=15, margin=72, width=92, lines_per_page=44):
    text = text.encode("latin-1", "replace").decode("latin-1")   # base-14 fonts are Latin-1
    lines = []
    for para in text.split("\n"):
        lines += textwrap.wrap(para, width) or [""]
    pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[""]]
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for page in pages:
        n = len(objs) + 1
        stream = [f"BT /F1 {size} Tf {lead} TL {margin} {792 - margin} Td"]
        stream += [f"({_esc(line)}) Tj T*" for line in page]
        stream.append("ET")
        body = "\n".join(stream).encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>".encode())
        kids.append(n + 1)
    objs[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as f:
        f.write(out)
