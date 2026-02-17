def chimidan_text(text: str) -> str:
    return text

def validate_comment(content: str) -> bool:
    """
    评论验证：必须大于5个字符
    """
    return len(content) > 5