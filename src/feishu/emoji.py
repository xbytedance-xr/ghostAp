class EmojiType:
    GET = "OnIt"
    TYPING = "Typing"
    THINKING = "THINKING"
    ONE_SEC = "OneSec"
    
    DONE = "DONE"
    CHECK_MARK = "CheckMark"
    LGTM = "LGTM"
    THUMBSUP = "THUMBSUP"
    OK = "OK"
    AWESOME = "AWESOME"
    
    CROSS_MARK = "CrossMark"
    ERROR = "ERROR"
    SOB = "SOB"
    WRONG = "WRONGED"
    
    SMART = "SMART"
    ROCKET = "Rocket"
    FIRE = "Fire"
    MUSCLE = "MUSCLE"
    YEAH = "YEAH"
    PARTY = "PARTY"
    
    SALUTE = "SALUTE"
    HIGHFIVE = "HIGHFIVE"
    WAVE = "WAVE"
    CLAP = "CLAP"
    
    FLASH = "StatusFlashOfInspiration"
    READING = "StatusReading"
    BUSY = "BusyStatus"


class EmojiReaction:
    @staticmethod
    def on_message_received() -> str:
        return EmojiType.GET
    
    @staticmethod
    def on_processing() -> str:
        return EmojiType.TYPING
    
    @staticmethod
    def on_thinking() -> str:
        return EmojiType.THINKING
    
    @staticmethod
    def on_success() -> str:
        return EmojiType.DONE
    
    @staticmethod
    def on_error() -> str:
        return EmojiType.SOB
    
    @staticmethod
    def on_coco_enter() -> str:
        return EmojiType.SMART
    
    @staticmethod
    def on_coco_exit() -> str:
        return EmojiType.WAVE
    
    @staticmethod
    def on_coco_response() -> str:
        return EmojiType.LGTM
    
    @staticmethod
    def on_multi_task_start() -> str:
        return EmojiType.ROCKET
    
    @staticmethod
    def on_multi_task_done() -> str:
        return EmojiType.PARTY
    
    @staticmethod
    def on_project_created() -> str:
        return EmojiType.FLASH
    
    @staticmethod
    def on_project_switched() -> str:
        return EmojiType.HIGHFIVE
    
    @staticmethod
    def on_dir_changed() -> str:
        return EmojiType.CHECK_MARK
    
    @staticmethod
    def on_shell_executed() -> str:
        return EmojiType.DONE
    
    @staticmethod
    def on_blocked() -> str:
        return EmojiType.CROSS_MARK

    @staticmethod
    def on_smart_mode() -> str:
        return EmojiType.OK

    @staticmethod
    def on_coco_mode() -> str:
        return EmojiType.GET
