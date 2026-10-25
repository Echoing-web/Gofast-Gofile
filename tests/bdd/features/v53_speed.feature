Feature: v5.3 提速与韧性 (SPEC §8 / G8 A8 A10)
  尾段饥饿时动态分裂分片保持并行度；服务器明示短 Retry-After 时
  熔断冷却按服务器信号执行而不被指数放大。

  Scenario: 尾段饥饿时动态分裂
    Given 一个带大尾段分片的可续传状态
    When 用4个工作线程执行下载
    Then 下载完成且内容正确
    And 尾段被至少3个并行请求覆盖

  Scenario: 短 Retry-After 风暴快速恢复
    Given 一个3分片的可下载文件
    And 服务器连续10次返回429并要求等待2秒
    When 执行下载
    Then 下载最终成功且文件内容完整
    And 熔断累计冷却远小于15分钟预算
