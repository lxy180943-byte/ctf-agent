import html
import json

from ctf_agent.analysis.php import analyze_php_text, extract_php_sources, lfi_replay_candidates


PHP_SOURCE = """<?php
highlight_file(__FILE__);
parse_str($_GET['raw'], $parsed);
if (md5($_GET['token']) == '0e462097431906509019562988736854') {
    $page = $_GET['page'];
}
if (in_array($_GET['role'], ['0', 'guest'])) { die('no'); }
if (intval($_GET['n'], 0) != 16) { die('bad'); }
if (strpos($page, 'php://') == true) { die('blocked'); }
if (preg_match('/flag|secret/i', $page)) { die('blocked'); }
include($page . '.php');
"""


def test_php_analyzer_extracts_parameters_and_strategy_library():
    analysis = analyze_php_text(PHP_SOURCE)
    names = {item['name'] for item in analysis.parameters}
    strategy_names = {item['name'] for item in analysis.strategies}

    assert {'raw', 'token', 'page', 'role', 'n'} <= names
    assert 'highlight_file' in analysis.dangerous_functions
    assert 'include' in analysis.dangerous_functions
    assert 'flag|secret' in analysis.blacklist_patterns
    assert 'parse_str-array-injection' in strategy_names
    assert 'md5-array-or-magic-hash' in strategy_names
    assert 'in_array-loose' in strategy_names
    assert 'intval-base-0' in strategy_names
    assert 'strpos-offset-bypass' in strategy_names
    assert 'lfi-php-wrapper' in strategy_names
    assert 'type-juggling-plus-lfi-chain' in strategy_names


def test_highlight_file_html_is_recovered_before_analysis():
    highlighted = '<code><span style="color: #000000">' + html.escape(PHP_SOURCE).replace('\n', '<br />') + '</span></code>'
    sources = extract_php_sources(highlighted)
    assert sources
    analysis = analyze_php_text(highlighted)
    assert any(item['name'] == 'page' for item in analysis.parameters)
    assert any(item['name'] == 'lfi-php-wrapper' for item in analysis.strategies)


def test_lfi_replay_candidates_evaluate_simple_php_echo(tmp_path):
    (tmp_path / 'index.php').write_text("<?php $page = $_GET['page']; include($page . '.php');", encoding='utf-8')
    (tmp_path / 'flag.php').write_text("<?php $prefix = 'flag'; echo $prefix . '{php_lfi_type_juggle}';", encoding='utf-8')

    candidates = lfi_replay_candidates(tmp_path / 'index.php', tmp_path)

    assert candidates[0]['target'] == 'flag.php'
    assert candidates[0]['simulated_output'] == 'flag{php_lfi_type_juggle}'
    assert 'php://filter/convert.base64-encode/resource=flag' == candidates[0]['payload']


def test_analysis_serializes_to_prompt_json():
    data = analyze_php_text(PHP_SOURCE).to_dict()
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert 'type-juggling-plus-lfi-chain' in encoded
