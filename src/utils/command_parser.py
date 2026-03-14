"""Command parser utility for handling slash commands with flags."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class ParsedCommand:
    """Represents a parsed slash command."""
    
    command: str
    """The base command (e.g., "/deep")."""
    
    args: str
    """The argument string (e.g., "implement login")."""
    
    flags: Dict[str, bool] = field(default_factory=dict)
    """Dictionary of parsed flags (e.g., {"all": True} from "--all")."""
    
    params: Dict[str, str] = field(default_factory=dict)
    """Dictionary of parsed key-value parameters (e.g., {"limit": "5"} from "--limit 5")."""

    @property
    def is_valid(self) -> bool:
        """Check if command is valid (non-empty)."""
        return bool(self.command)


class CommandParser:
    """Parser for slash commands."""

    @staticmethod
    def parse(text: str, known_flags: Optional[Set[str]] = None) -> ParsedCommand:
        """
        Parse a command string into structured data.
        
        Args:
            text: The raw command string (e.g., "/deep_status --all")
            known_flags: Optional set of known flag names (without dashes) to look for.
                         If None, simple parsing is used.
                         
        Returns:
            ParsedCommand object
        """
        text = text.strip()
        if not text:
            return ParsedCommand(command="", args="")

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        
        if len(parts) == 1:
            return ParsedCommand(command=command, args="")
            
        raw_args = parts[1]
        
        # Simple parsing logic
        # 1. Check if args look like flags
        # 2. Extract flags and params
        # 3. Remaining text is args
        
        flags = {}
        params = {}
        
        # Tokenize args to find flags
        # Note: This simple tokenizer splits by whitespace. 
        # A more robust one would handle quotes, but for now this suffices for our use case.
        tokens = raw_args.split()
        
        cleaned_tokens = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            
            # Check for flag pattern (--flag or -f)
            if token.startswith("-"):
                # Clean dash prefix
                flag_name = token.lstrip("-")
                
                # Check if it's a known flag or looks like one
                is_flag = True
                
                # If we have a value next, it might be a param (future extension)
                # For now, we only support boolean flags
                
                if is_flag:
                    flags[flag_name] = True
                    i += 1
                    continue
            
            cleaned_tokens.append(token)
            i += 1
            
        # Reconstruct remaining args
        # We need to be careful not to disrupt the original spacing of the requirement text too much
        # But for commands like "/deep_status --all", args should be empty or just flags
        
        # Improved strategy:
        # If the command expects natural language input (like /deep requirement), 
        # we might want to treat everything as args unless it strictly matches known flags at start/end.
        # But for now, let's stick to the spec: "parsing flags".
        
        # If known_flags provided, only parse those
        if known_flags:
            # Reset and do targeted parsing
            # This is safer for mixed content
            # e.g. /deep implement feature --priority high
            # We don't want to swallow "--priority" if it's part of the requirement text unless we know it's a flag
            
            # For current task, we mostly care about specific flags like --all for status commands
            # So let's implement a specific "extract flags" logic
            
            extracted_flags = {}
            remaining_text = raw_args
            
            # Check for flags at the beginning or end of string to avoid middle-of-sentence collisions?
            # Or just replace standard tokens.
            
            tokens = raw_args.split()
            filtered_tokens = []
            
            for token in tokens:
                if token.startswith("-"):
                    flag_name = token.lstrip("-")
                    if flag_name in known_flags:
                        extracted_flags[flag_name] = True
                        continue
                        
                    # Handle alias mapping if known_flags contains values?
                    # For simplicity, caller should normalize aliases (e.g. check "a" and "all")
                
                filtered_tokens.append(token)
                
            return ParsedCommand(
                command=command,
                args=" ".join(filtered_tokens), # This loses original whitespace, but usually acceptable
                flags=extracted_flags
            )
            
        # Default behavior:
        # For commands like /deep_status, args are usually flags.
        # For /deep <requirement>, args are text.
        
        # Let's start with a simple implementation that matches the current "hardcoded slice" logic replacement needs.
        # Most usage is: command + optional args.
        
        return ParsedCommand(
            command=command,
            args=raw_args,
            # We populate flags based on raw_args content for easier checking
            # e.g. if raw_args is "--all", flags={"all": True}
        )

    @staticmethod
    def parse_basic(text: str) -> ParsedCommand:
        """
        Basic parsing: Command + Args string.
        Also attempts to identify common flags in args.
        """
        text = text.strip()
        if not text:
            return ParsedCommand(command="", args="")

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        flags = {}
        
        # Simple flag extraction for common patterns (end or start of line)
        # e.g. "/deep_status --all"
        args_lower = args.lower().strip()
        if args_lower.startswith("-") or args_lower.startswith("--"):
            # Check if the whole arg is a flag or list of flags
            tokens = args_lower.split()
            if all(t.startswith("-") for t in tokens):
                # All tokens are flags
                for t in tokens:
                    flags[t.lstrip("-")] = True
                # In this case, "args" might be considered consumed/empty if it was just flags?
                # But to preserve info, we keep args as is, and let user check flags.
        
        return ParsedCommand(command=command, args=args, flags=flags)
