import { describe, expect, it } from 'vitest';
import type { CommandSpec } from '../types';
import {
  commandNeedsArgument,
  inlineCommandArgs,
  missingArgumentMessage,
  parseCommandArgs,
} from './commandArgs';

function spec(overrides: Partial<CommandSpec> = {}): CommandSpec {
  return {
    name: 'rename',
    aliases: [],
    summary: 'rename the current session',
    usage: '/rename <title>',
    group: 'session',
    takes_arg: false,
    addressable: true,
    ...overrides,
  };
}

describe('parseCommandArgs', () => {
  it('splits plain arguments on whitespace', () => {
    expect(parseCommandArgs('  alpha   beta\n gamma  ')).toEqual({
      args: ['alpha', 'beta', 'gamma'],
      error: null,
    });
  });

  it('keeps Windows paths with backslashes intact', () => {
    expect(parseCommandArgs(String.raw`"C:\Program Files\My App" D:\workspace`)).toEqual({
      args: [String.raw`C:\Program Files\My App`, String.raw`D:\workspace`],
      error: null,
    });
  });

  it('does not treat apostrophes inside a word as a quote', () => {
    expect(parseCommandArgs(String.raw`C:\Users\O'Brien\repo`)).toEqual({
      args: [String.raw`C:\Users\O'Brien\repo`],
      error: null,
    });
  });

  it('reports an unterminated quote instead of truncating', () => {
    expect(parseCommandArgs('"C:\Program Files')).toEqual({
      args: [],
      error: 'Missing closing quote in the command arguments.',
    });
  });
});

describe('commandNeedsArgument', () => {
  it('honours the server flag', () => {
    expect(commandNeedsArgument(spec({ takes_arg: true }))).toBe(true);
  });

  it('detects a required argument from the usage string', () => {
    expect(commandNeedsArgument(spec())).toBe(true);
    expect(commandNeedsArgument(spec({ usage: '/sessions' }))).toBe(false);
  });
});

describe('inlineCommandArgs', () => {
  it('parses arguments typed after the command name', () => {
    expect(inlineCommandArgs(spec(), '/rename Release notes')).toEqual({
      raw: 'Release notes',
      parsed: { args: ['Release', 'notes'], error: null },
    });
  });

  it('returns null when only the command is present', () => {
    expect(inlineCommandArgs(spec(), '/rename')).toBeNull();
  });

  it('resolves aliases', () => {
    expect(inlineCommandArgs(spec({ aliases: ['mv'] }), '/mv next name')).toEqual({
      raw: 'next name',
      parsed: { args: ['next', 'name'], error: null },
    });
  });
});

describe('missingArgumentMessage', () => {
  it('includes the usage string', () => {
    expect(missingArgumentMessage(spec())).toBe('Missing argument for /rename. Usage: /rename <title>');
  });
});
