# CTF Agent Docker Profiles

These images are for authorized CTF challenges, local labs, competition targets, and benchmarks only.

Build all local profiles:

```bash
make docker-build
```

Build one profile:

```bash
ctf-agent docker build --profile pwn
```

Check images:

```bash
ctf-agent docker doctor --run-tools
```

Profiles:

- `ctf-agent:generic`: Python, file, binutils, curl, wget, ripgrep, jq, xxd, netcat-openbsd.
- `ctf-agent:pwn`: gdb, gdbserver, checksec, Ubuntu-packaged pwntools and ROPgadget. `one_gadget` is documented as optional.
- `ctf-agent:web`: curl, nmap, ffuf, sqlmap, Python requests/httpx.
- `ctf-agent:crypto`: Python, sympy, pycryptodome via the `Cryptodome` import name, python3-z3/z3. Sage stays in a separate optional image/profile.
- `ctf-agent:rev`: binutils, radare2, gdb, strings, ltrace, strace.
- `ctf-agent:forensics`: binwalk, exiftool, foremost, pngcheck, steghide. zsteg installation notes are in the image.
