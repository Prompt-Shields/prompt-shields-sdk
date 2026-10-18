#!/usr/bin/env node
/**
 * Release guard for @prompt-shields/partner.
 *
 *   node scripts/check-version.js                 # package.json and src/client.ts agree
 *   node scripts/check-version.js partner-ts-v0.2.0  # ...and the tag names that version
 *
 * Exits non-zero with a message on the first mismatch.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const clientSrc = fs.readFileSync(path.join(root, 'src', 'client.ts'), 'utf8');

function fail(msg) {
  console.error(`check-version: ${msg}`);
  process.exit(1);
}

const match = clientSrc.match(/export const VERSION = '([^']+)';/);
if (!match) fail("src/client.ts has no `export const VERSION = '...';`");
if (match[1] !== pkg.version) {
  fail(`src/client.ts VERSION is ${match[1]} but package.json version is ${pkg.version}`);
}

const tag = process.argv[2];
if (tag !== undefined) {
  const prefix = 'partner-ts-v';
  if (!tag.startsWith(prefix)) fail(`tag ${tag} does not start with ${prefix}`);
  const tagVersion = tag.slice(prefix.length);
  if (tagVersion !== pkg.version) {
    fail(`tag ${tag} is version ${tagVersion} but package.json version is ${pkg.version}`);
  }
}

console.log(`check-version: ${pkg.name}@${pkg.version}${tag ? ` matches ${tag}` : ''}`);
