from ctf_agent.analysis.observation import ObservationSummarizer

def test_highlight_file_response_becomes_evidence_package():
    text = "HTTP/1.1 200 OK\nContent-Type: text/html\n\n<title>Source</title><form method=post action=/check><input name=token><input name=page></form><a href=/robots.txt>robots</a><pre>&lt;?php if (md5([\x27token\x27]) == \x270e1\x27) { include([\x27page\x27]); } ?&gt;</pre>"
    evidence = ObservationSummarizer().summarize(text, timed_out=True)
    assert evidence["status"] == 200
    assert evidence["headers"]["content-type"] == "text/html"
    assert evidence["title"] == "Source"
    assert evidence["forms"][0]["inputs"][0]["name"] == "token"
    assert evidence["links"] == ["/robots.txt"]
    assert evidence["timed_out"] is True
    php = evidence["php_analysis"]
    assert "include" in php["sinks"]
    assert php["candidate_strategies"]

def test_partial_response_without_headers_still_has_body():
    evidence = ObservationSummarizer().summarize("partial body before timeout", timed_out=True)
    assert evidence["body_excerpt"] == "partial body before timeout"
    assert evidence["timed_out"] is True
