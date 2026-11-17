"""
自动词典提取器

在文档入库后，自动从新文本中提取高频专有名词，
追加到 jieba 自定义词典并热加载，无需重启服务。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 过滤掉无意义的停用词和单字
_STOP_WORDS = {
    # 虚词介词
    "的", "了", "是", "在", "有", "和", "与", "或", "等", "为", "对",
    "中", "从", "到", "被", "把", "将", "可以", "可", "能", "都", "也",
    "该", "这", "那", "其", "各", "及", "按", "经", "通过", "由", "于",
    "之", "以", "而", "但", "如", "则", "因", "所", "用", "不", "无",
    # 通用动词
    "使用", "查询", "登录", "输入", "点击", "选择", "填写", "确认", "提交",
    "下载", "安装", "设置", "修改", "删除", "添加", "完成", "开始", "结束",
    "处理", "操作", "进行", "提供", "支持", "联系", "相关",
    # 通用名词
    "产品", "商品", "用户", "客户", "服务", "问题", "情况", "方式", "方法",
    "时间", "日期", "系统", "平台", "页面", "界面", "功能", "版本", "内容",
    "数据", "信息", "结果", "状态", "记录", "详情", "说明", "备注", "描述",
    "标题", "类型", "数量", "金额", "价格", "费用", "步骤", "流程", "规则",
    # 时间词
    "今天", "明天", "昨天", "当天", "当前", "目前", "现在", "最近", "之前",
    "工作日", "自然日", "小时", "分钟",
    # 电商通用词（已在手动词典中）
    "退货", "退款", "换货", "发货", "收货", "下单", "付款", "订单",
}


class DictExtractor:
    """
    从入库文本中自动提取高频专有名词，追加到 jieba 自定义词典。

    策略：
    1. 用 jieba TF-IDF 提取关键词（候选词）
    2. 过滤掉停用词、单字、纯数字
    3. 与已有词典对比，只追加新词
    4. 写入词典文件（持久化）
    5. jieba.add_word() 热加载（立即生效）
    """

    def __init__(self, dict_path: str = "data/dicts/custom.txt") -> None:
        self.dict_path = Path(dict_path)
        self._existing_words: set[str] = set()
        self._load_existing_words()

    def _load_existing_words(self) -> None:
        """读取词典文件中已有的词语。"""
        if not self.dict_path.exists():
            return
        with open(self.dict_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    word = line.split()[0]
                    self._existing_words.add(word)
        logger.debug("已加载词典词条数: %d", len(self._existing_words))

    def _is_valid_word(self, word: str) -> bool:
        """判断候选词是否值得加入词典。"""
        if len(word) < 2:
            return False
        if word in _STOP_WORDS:
            return False
        if re.fullmatch(r'[\d\W]+', word):
            return False  # 纯数字或纯符号
        if word in self._existing_words:
            return False  # 已在词典中
        return True

    def extract_and_update(
        self,
        texts: list[str],
        top_k: int = 30,
        min_freq: int = 2,
        word_freq: int = 100,
    ) -> list[str]:
        """
        从文本列表中提取新词并更新词典。

        Args:
            texts:    入库的原始文本列表
            top_k:    每批最多提取的新词数量
            min_freq: TF-IDF 关键词最小出现频次
            word_freq: 写入词典的词频（越高优先级越高）

        Returns:
            本次新增的词语列表
        """
        try:
            import jieba
            import jieba.analyse
        except ImportError:
            logger.warning("jieba 未安装，跳过词典自动提取")
            return []

        # 合并所有文本，用 TF-IDF 提取关键词
        combined = "\n".join(texts)
        candidates = jieba.analyse.extract_tags(
            combined,
            topK=top_k * 3,   # 多提取一些，过滤后取 top_k
            withWeight=True,
        )

        # 统计词频（用于 min_freq 过滤）
        from collections import Counter
        word_counter: Counter = Counter()
        for text in texts:
            tokens = jieba.cut(text, cut_all=False)
            word_counter.update(t for t in tokens if t.strip())

        # 筛选有效新词
        new_words: list[str] = []
        for word, _ in candidates:
            if len(new_words) >= top_k:
                break
            if self._is_valid_word(word) and word_counter.get(word, 0) >= min_freq:
                new_words.append(word)

        if not new_words:
            logger.info("[词典提取] 未发现新词")
            return []

        # 写入词典文件（持久化）
        self._append_to_dict_file(new_words, word_freq)

        # 热加载到 jieba（立即生效，无需重启）
        for word in new_words:
            jieba.add_word(word, freq=word_freq, tag="n")
            self._existing_words.add(word)

        logger.info(
            "[词典提取] 新增 %d 个词: %s",
            len(new_words),
            "、".join(new_words[:10]) + ("..." if len(new_words) > 10 else "")
        )
        return new_words

    def _append_to_dict_file(self, words: list[str], freq: int) -> None:
        """将新词追加到词典文件末尾。"""
        self.dict_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dict_path, "a", encoding="utf-8") as f:
            f.write("\n# ── 自动提取 ──────────────────────────────────────────\n")
            for word in words:
                f.write(f"{word} {freq} n\n")
        logger.debug("[词典提取] 已追加 %d 词到 %s", len(words), self.dict_path)


# 全局单例（避免重复初始化）
_extractor: Optional[DictExtractor] = None


def get_extractor(dict_path: str = "data/dicts/custom.txt") -> DictExtractor:
    """获取词典提取器单例。"""
    global _extractor
    if _extractor is None:
        _extractor = DictExtractor(dict_path=dict_path)
    return _extractor
