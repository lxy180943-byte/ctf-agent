from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ctf_agent.core.models import Challenge


@dataclass
class CategoryClassification:
    category: str
    scores: dict[str, int] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


class CategoryClassifier:
    CATEGORIES = ("pwn", "web", "crypto", "rev", "forensics", "misc")

    KEYWORDS = {
        "pwn": ("pwn", "overflow", "heap", "rop", "shellcode", "libc", "canary", "ret2"),
        "web": ("web", "http", "https", "url", "cookie", "xss", "sqli", "sql", "csrf", "jwt"),
        "crypto": ("crypto", "rsa", "aes", "xor", "cipher", "decrypt", "encrypt", "modulus", "prime", "oracle"),
        "rev": ("rev", "reverse", "reversing", "apk", "wasm", "decompile", "binary", "license"),
        "forensics": ("forensics", "pcap", "image", "stego", "metadata", "memory", "dump", "zip", "pdf"),
    }

    EXTENSIONS = {
        "pwn": (".elf",),
        "web": (".html", ".js", ".php", ".har"),
        "crypto": (".pem", ".pub", ".key"),
        "rev": (".so", ".dll", ".exe", ".apk", ".wasm", ".class", ".jar"),
        "forensics": (".pcap", ".pcapng", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".7z", ".pdf", ".wav"),
    }

    def classify(self, challenge: Challenge, challenge_dir: str | Path | None = None) -> CategoryClassification:
        scores = {category: 0 for category in self.CATEGORIES}
        evidence: list[str] = []

        category = challenge.category.lower()
        if category in scores and category != "misc":
            scores[category] += 3
            evidence.append(f"metadata category={challenge.category}")

        text = " ".join([challenge.title, challenge.description, " ".join(challenge.hints), str(challenge.connection or "")]).lower()
        for cat, keywords in self.KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    scores[cat] += 1
                    evidence.append(f"keyword {keyword}->{cat}")

        if challenge.connection and any(token in challenge.connection.lower() for token in ("http://", "https://")):
            scores["web"] += 3
            evidence.append("http connection->web")
        elif challenge.connection:
            scores["pwn"] += 1
            evidence.append("raw connection->pwn")

        base = Path(challenge_dir).expanduser() if challenge_dir else None
        for file_name in challenge.files:
            suffix = Path(file_name).suffix.lower()
            for cat, suffixes in self.EXTENSIONS.items():
                if suffix in suffixes:
                    scores[cat] += 2
                    evidence.append(f"extension {suffix}->{cat}")
            if base:
                self._score_magic(base / file_name, scores, evidence)

        best = max(scores, key=lambda item: scores[item])
        if scores[best] <= 0:
            best = "misc"
        return CategoryClassification(category=best, scores=scores, evidence=evidence)

    def _score_magic(self, path: Path, scores: dict[str, int], evidence: list[str]) -> None:
        try:
            magic = path.read_bytes()[:16]
        except OSError:
            return
        if magic.startswith(b"\x7fELF"):
            scores["pwn"] += 2
            scores["rev"] += 2
            evidence.append("magic ELF->pwn/rev")
        elif magic.startswith(b"\x89PNG") or magic.startswith(b"\xff\xd8\xff"):
            scores["forensics"] += 3
            evidence.append("magic image->forensics")
        elif magic.startswith(b"PK\x03\x04"):
            scores["forensics"] += 2
            evidence.append("magic zip->forensics")
        elif magic.startswith(b"%PDF"):
            scores["forensics"] += 2
            evidence.append("magic pdf->forensics")
        elif magic[:4] in {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x0a\x0d\x0d\x0a"}:
            scores["forensics"] += 3
            evidence.append("magic pcap->forensics")
