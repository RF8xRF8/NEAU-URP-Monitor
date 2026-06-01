from monitor import _analyze_login_failure, _response_needs_slider_captcha, _slider_move_length


def test_slider_move_length_uses_target_center_and_tag_width():
    match_result = {"target_x": 372}

    assert _slider_move_length(match_result, 93, 590) == 154.40677966101694


def test_slider_related_failure_is_retryable():
    retryable, reason = _analyze_login_failure("请完成安全验证！向右滑动填充拼图", "https://example.com/login")

    assert retryable is True
    assert "验证码识别失败" in reason


def test_slider_captcha_trigger_only_on_captcha_response():
    assert _response_needs_slider_captcha("请完成安全验证！", "https://example.com/authserver/login") is True
    assert _response_needs_slider_captcha("登录成功", "https://example.com/portal") is False


def test_slider_captcha_trigger_can_follow_401_response():
    assert _response_needs_slider_captcha("验证码错误，请重试", "https://example.com/authserver/login") is True