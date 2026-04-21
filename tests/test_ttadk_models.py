from src.ttadk.models import ACPModelOption, ACPToolOption


def test_acp_tool_option_defaults():
    opt = ACPToolOption(name="coco")

    assert opt.name == "coco"
    assert opt.description == ""
    assert opt.is_default is False
    # 默认 emoji 应为机器人图标，方便卡片统一展示
    assert opt.emoji == "🤖"


def test_acp_model_option_defaults():
    opt = ACPModelOption(name="gpt-5.2")

    assert opt.name == "gpt-5.2"
    assert opt.description == ""
    assert opt.is_default is False


def test_acp_model_option_is_default_flag():
    opt = ACPModelOption(name="gpt-5.2-pro", description="Primary model", is_default=True)

    assert opt.is_default is True
    assert opt.description == "Primary model"

