<?php
highlight_file(__FILE__);
parse_str($_GET['raw'] ?? '', $parsed);
$token = $_GET['token'] ?? '';
$page = $_GET['page'] ?? 'home';
$role = $_GET['role'] ?? 'guest';
$n = $_GET['n'] ?? '0';
if (md5($token) == '0e462097431906509019562988736854' && in_array($role, array('0', 'guest')) && intval($n, 0) == 16) {
    if (strpos($page, 'php://') == true) {
        die('blocked wrapper');
    }
    if (preg_match('/secret|flag/i', $page)) {
        die('blocked name');
    }
    include($page . '.php');
}
