// Prints the exported API surface of TypeScript entry files as JSON:
// {"surface": {"entry:Name": "signature"}, "sigs": {"entry:Name": {params, ret, typeParams}}}.
// Usage: node ts_extract.js <root> <entry> [<entry> ...]
// Uses the project's own `typescript` package (or COVENANT_TYPESCRIPT, a path to one).
"use strict";
const path = require("path");

const [, , root, ...entries] = process.argv;
let ts;
try {
  const where = process.env.COVENANT_TYPESCRIPT || require.resolve("typescript", { paths: [root, process.cwd()] });
  ts = require(where);
} catch (e) {
  console.error("typescript not found; install it in the project (npm i -D typescript)");
  process.exit(3);
}

function compilerOptions() {
  const configPath = ts.findConfigFile(root, ts.sys.fileExists, "tsconfig.json");
  const defaults = {
    allowJs: true,
    noEmit: true,
    skipLibCheck: true,
    strict: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler || ts.ModuleResolutionKind.NodeJs,
  };
  if (!configPath || !path.resolve(configPath).startsWith(path.resolve(root))) return defaults;
  const read = ts.readConfigFile(configPath, ts.sys.readFile);
  if (read.error) return defaults;
  const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, path.dirname(configPath));
  return { ...parsed.options, noEmit: true, skipLibCheck: true };
}

const files = entries.map((e) => path.resolve(root, e));
const program = ts.createProgram(files, compilerOptions());
const checker = program.getTypeChecker();
const F = ts.TypeFormatFlags;
const flags = F.NoTruncation | F.WriteArrowStyleSignature;
const rootPatterns = [path.resolve(root), path.resolve(root).split(path.sep).join("/")];

function clean(text) {
  for (const p of rootPatterns) text = text.split(p).join("<root>");
  return text;
}

function isHidden(decl) {
  if (!decl) return false;
  const mods = ts.getCombinedModifierFlags(decl);
  return (mods & (ts.ModifierFlags.Private | ts.ModifierFlags.Protected)) !== 0;
}

// Structured call signatures, so the checker can tell additive changes from breaking ones.
const sigs = {};

// TypeScript caps printed types near a million characters even with NoTruncation, and where it
// stops ("... 6 more ...") varies between runs. A shortened type can't be compared, so compare
// the declaration's own source text instead: stable, and it changes when the declaration does.
const TRUNCATED = /\.\.\. \d+ more \.\.\./;
function show(type, decl, extraFlags = 0, enclosing = undefined) {
  const text = clean(checker.typeToString(type, enclosing, flags | extraFlags));
  // Past ~20k characters, also use the declaration: near the cap one side may be cut and the other not.
  if ((!TRUNCATED.test(text) && text.length < 20000) || !decl) return text;
  return `declared as ${clean(decl.getText().replace(/\s+/g, " "))}`;
}

function paramInfo(p) {
  const d = p.valueDeclaration;
  const optional = !!(d && (d.questionToken || d.initializer));
  const rest = !!(d && d.dotDotDotToken);
  const type = d && d.type
    ? clean(d.type.getText())
    : clean(checker.typeToString(checker.getTypeOfSymbolAtLocation(p, d), undefined, flags));
  return { type, optional, rest };
}

function recordSigs(key, signatures) {
  if (signatures.length !== 1) return; // overloads: compared as text only
  const sig = signatures[0];
  sigs[key] = {
    params: sig.getParameters().map(paramInfo),
    ret: show(sig.getReturnType(), sig.getDeclaration()),
    typeParams: (sig.getTypeParameters() || []).map((tp) => clean(checker.typeToString(tp, undefined, flags))),
  };
}

function members(out, prefix, type, decl) {
  for (const prop of checker.getPropertiesOfType(type)) {
    const pdecl = prop.valueDeclaration || (prop.declarations || [])[0];
    if (prop.getName().startsWith("#") || isHidden(pdecl)) continue;
    const optional = prop.flags & ts.SymbolFlags.Optional ? "?" : "";
    const readonly = pdecl && ts.getCombinedModifierFlags(pdecl) & ts.ModifierFlags.Readonly ? "readonly " : "";
    const ptype = checker.getTypeOfSymbolAtLocation(prop, pdecl || decl);
    out[`${prefix}.${prop.getName()}`] = `${readonly}${optional}${show(ptype, pdecl)}`;
    if (!optional) recordSigs(`${prefix}.${prop.getName()}`, ptype.getCallSignatures());
  }
}

const out = {};
let failed = false;
files.forEach((file, i) => {
  const sf = program.getSourceFile(file);
  if (!sf) {
    console.error(`entry not found: ${entries[i]}`);
    failed = true;
    return;
  }
  const moduleSymbol = checker.getSymbolAtLocation(sf);
  if (!moduleSymbol) return;
  for (const exported of checker.getExportsOfModule(moduleSymbol)) {
    let sym = exported;
    if (sym.flags & ts.SymbolFlags.Alias) sym = checker.getAliasedSymbol(sym);
    const name = `${entries[i]}:${exported.getName()}`;
    const decl = sym.valueDeclaration || (sym.declarations || [])[0];
    const S = ts.SymbolFlags;
    if (sym.flags & S.Class) {
      const ctor = checker.getTypeOfSymbolAtLocation(sym, decl);
      const ctorSigs = ctor.getConstructSignatures();
      out[name] = `class new${ctorSigs.map((s) => clean(checker.signatureToString(s, undefined, flags))).join(" | new") || "()"}`;
      recordSigs(name, ctorSigs);
      members(out, name, checker.getDeclaredTypeOfSymbol(sym), decl);
      members(out, `${name}.static`, ctor, decl);
      delete out[`${name}.static.prototype`];
    } else if (sym.flags & (S.Interface | S.TypeAlias)) {
      const declared = checker.getDeclaredTypeOfSymbol(sym);
      const kind = sym.flags & S.Interface ? "interface" : "type";
      out[name] = `${kind} ${show(declared, decl, F.InTypeAlias, decl)}`;
      if (sym.flags & S.Interface) members(out, name, declared, decl);
    } else if (sym.flags & S.Enum) {
      out[name] = "enum";
      for (const m of sym.exports ? Array.from(sym.exports.keys()) : []) out[`${name}.${m}`] = "member";
    } else if (sym.flags & (S.Function | S.Variable | S.ValueModule)) {
      const type = checker.getTypeOfSymbolAtLocation(sym, decl);
      const kind = sym.flags & S.Function ? "function" : sym.flags & S.ValueModule ? "namespace" : "const";
      out[name] = `${kind} ${show(type, decl)}`;
      recordSigs(name, type.getCallSignatures());
    } else {
      out[name] = "export";
    }
  }
});
process.stdout.write(JSON.stringify({ surface: out, sigs }));
process.exit(failed ? 4 : 0);
