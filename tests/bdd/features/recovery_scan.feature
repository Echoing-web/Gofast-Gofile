Feature: 恢复扫描 (SPEC S5 / D9 修复)
  清单丢失后可从 partial 重建进度：只有确认含非零数据的分片计为完成，
  全零分片（预分配空洞）必须重新下载——修复 v5.1 补丁的字面量 NUL bug。

  Scenario: 混合内容的部分文件被正确分类
    Given 一个3分片的partial文件其中第2片全零
    When 执行恢复扫描
    Then 清单标记为 DONE WAIT DONE
    And partial文件内容未被修改

  Scenario: 采样窗口之间的数据孤岛被保守处理
    Given 一个3分片的partial文件其中第3片只在采样窗口之间有数据
    When 执行恢复扫描
    Then 第3片被保守判定为WAIT（宁重下不交付空洞）
