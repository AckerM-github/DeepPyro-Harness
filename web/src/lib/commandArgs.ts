import type { CommandSpec } from '../types';

/** Result of tokenising the free-form text typed in the palette argument step. */
export interface ParsedCommandArgs {
  args: string[];
  /** Human readable problem, or `null` when the text parsed cleanly. */
  error: string | null;
}

/** Inline arguments typed after the command name in the palette search field. */
export interface InlineCommandArgs {
  raw: string;
  parsed: ParsedCommandArgs;
}

/** Usage strings mark a genuinely required argument with angle brackets, e.g. "/rename <title>". */
const REQUIRED_ARGUMENT = /<[^>]+>/;

/**
 * Split the argument text typed in the command palette into argv tokens.
 *
 * The parser is deliberately shell-like but forgiving for Windows paths:
 * - whitespace separates arguments;
 * - a single or double quote starts a quoted group only at the start of a token,
 *   so apostrophes inside a word (e.g. `C:\Users\O'Brien`) and literal quotes
 *   elsewhere stay in the argument;
 * - backslashes are always literal (never escape characters), which keeps
 *   `"C:\Program Files\My App"` and a trailing `\\` intact;
 * - an unterminated quote is reported as an error instead of producing a
 *   truncated argument.
 */
export function parseCommandArgs(text: string): ParsedCommandArgs {
  const args: string[] = [];
  let current = '';
  let tokenStarted = false;
  let quote = '';
  for (const char of text.trim()) {
    if (quote) {
      if (char === quote) quote = '';
      else current += char;
      continue;
    }
    if (char === '"' || char === "'") {
      if (!tokenStarted) {
        quote = char;
        tokenStarted = true;
      } else {
        current += char;
        tokenStarted = true;
      }
      continue;
    }
    if (/\s/.test(char)) {
      if (tokenStarted) {
        if (current) args.push(current);
        current = '';
        tokenStarted = false;
      }
      continue;
    }
    current += char;
    tokenStarted = true;
  }
  if (quote) {
    return { args, error: 'Missing closing quote in the command arguments.' };
  }
  if (tokenStarted && current) args.push(current);
  return { args, error: null };
}

/**
 * Commands that need an explicit argument step. `takes_arg` is the server flag and
 * is derived from the presence of `[` in the usage string, so required arguments
 * written as `<name>` (e.g. `/rename <title>`) have to be detected from usage too.
 */
export function commandNeedsArgument(command: CommandSpec): boolean {
  return command.takes_arg || REQUIRED_ARGUMENT.test(command.usage);
}

/** Message shown when Enter is pressed in the argument step without a usable argument. */
export function missingArgumentMessage(command: CommandSpec): string {
  const usage = command.usage.trim();
  if (!usage) return `Missing argument for /${command.name}.`;
  return `Missing argument for /${command.name}. Usage: ${usage}`;
}

/**
 * Arguments already typed after the command name in the search field,
 * e.g. "/model gpt-4" -> raw "gpt-4", parsed ["gpt-4"].
 * Returns `null` when the field holds only the command invocation (no trailing text).
 */
export function inlineCommandArgs(command: CommandSpec, query: string): InlineCommandArgs | null {
  const body = query.trim().replace(/^\/+/, '');
  if (!body) return null;
  const lower = body.toLowerCase();
  const forms = [command.name, ...command.aliases]
    .map((form) => form.toLowerCase())
    .filter((form) => form.length > 0)
    .sort((a, b) => b.length - a.length);
  for (const form of forms) {
    if (lower === form) return null;
    if (lower.startsWith(form) && /\s/.test(body.charAt(form.length))) {
      const raw = body.slice(form.length).trim();
      return { raw, parsed: parseCommandArgs(raw) };
    }
  }
  return null;
}
