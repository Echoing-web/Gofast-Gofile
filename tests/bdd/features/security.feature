Feature: 安全边界 (SPEC S6 / D13, Mimosa 约束)
  仅允许 http/https 且目标必须是公网地址；逐跳重定向同样受检；
  私有/环回/保留地址一律拒绝，且不发出任何请求。

  Scenario: 私网直链被拒绝
    Given 一个解析到10.0.0.5的直链
    When 尝试校验该直链
    Then 校验以致命错误拒绝

  Scenario: 非http协议被拒绝
    Given 一个ftp协议的直链
    When 尝试校验该直链
    Then 校验以致命错误拒绝

  Scenario: 重定向跳向环回地址被拦截
    Given 一个会302跳转到127.0.0.1的直链
    When 通过传输层发起请求
    Then 请求以致命错误终止
    And 环回地址从未收到任何请求
