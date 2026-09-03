from ctf_agent.knowledge import SkillIndex

def test_skill_index_retrieves_php_notes(tmp_path):
    root = tmp_path / "skills"
    path = root / "ctf-web" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# PHP\nparse_str arrays, md5(array), weak comparison, intval base 0, strpos, php://filter and LFI", encoding="utf-8")
    notes = SkillIndex(root, snippet_limit=80).search("PHP parse_str md5 array weak comparison intval base 0 strpos php://filter LFI", category="web")
    assert notes and "parse_str" in notes[0]["snippet"]
    assert "php://filter" in notes[0]["snippet"]
    assert len(notes[0]["snippet"]) <= 123

def test_skill_index_missing_root_is_offline_safe(tmp_path):
    assert SkillIndex(tmp_path / "missing").search("php LFI") == []

def test_skill_index_expands_chinese_ctf_query(tmp_path):
    root = tmp_path / "skills" / "ctf-web"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("# PHP\nType juggling, parse_str arrays, LFI and php://filter.", encoding="utf-8")
    notes = SkillIndex(tmp_path / "skills").search("PHP特性+文件包含", category="web")
    assert notes and "php://filter" in notes[0]["snippet"]
