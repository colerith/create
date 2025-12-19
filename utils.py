def chimidan_text(text: str) -> str:
    """
    已移除奇米蛋风格转换，直接返回原文本。
    保留此函数名是为了兼容其他文件中的调用，防止报错。
    """
    return text

def validate_comment(content: str) -> bool:
    """
    评论验证：必须大于5个字符
    """
    return len(content) > 5