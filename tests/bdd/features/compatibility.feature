Feature: v5.1 向后兼容 (SPEC S7 / G5)
  v5.1 的清单、命令行参数与子命令在 v5.2 中全部可用。

  Scenario: v5.1 清单可被加载续传并最终完成
    Given 一个3分片的可下载文件
    And 一个v5.1格式的清单记录了1个已完成分片
    When 用v5.2执行下载
    Then 下载完成且内容正确

  Scenario: v5.1 命令行参数仍然有效
    When 解析v5.1风格的download命令行
    Then workers为32且auto_workers为真且segment_mib为128

  Scenario: v5.2 产物清单可被v5.1语义读取
    Given 一份v5.2保存的清单
    Then 其JSON结构可被v5.1加载逻辑还原为分片列表
