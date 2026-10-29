// Prints the exported API surface of TypeScript entry files as JSON: {"entry:Name": "signature"}.
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

function members(out, prefix, type, decl) {
  for (const prop of checker.getPropertiesOfType(type)) {
    const pdecl = prop.valueDeclaration || (prop.declarations || [])[0];
    if (prop.getName().startsWith("#") || isHidden(pdecl)) continue;
    const optional = prop.flags & ts.SymbolFlags.Optional ? "?" : "";
    const readonly = pdecl && ts.getCombinedModifierFlags(pdecl) & ts.ModifierFlags.Readonly ? "readonly " : "";
    const ptype = checker.getTypeOfSymbolAtLocation(prop, pdecl || decl);
    out[`${prefix}.${prop.getName()}`] = `${readonly}${optional}${clean(checker.typeToString(ptype, undefined, flags))}`;
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
      const sigs = ctor.getConstructSignatures().map((s) => clean(checker.signatureToString(s, undefined, flags)));
      out[name] = `class new${sigs.join(" | new") || "()"}`;
      members(out, name, checker.getDeclaredTypeOfSymbol(sym), decl);
      members(out, `${name}.static`, ctor, decl);
      delete out[`${name}.static.prototype`];
    } else if (sym.flags & (S.Interface | S.TypeAlias)) {
      const declared = checker.getDeclaredTypeOfSymbol(sym);
      const kind = sym.flags & S.Interface ? "interface" : "type";
      out[name] = `${kind} ${clean(checker.typeToString(declared, decl, flags | F.InTypeAlias))}`;
      if (sym.flags & S.Interface) members(out, name, declared, decl);
    } else if (sym.flags & S.Enum) {
      out[name] = "enum";
      for (const m of sym.exports ? Array.from(sym.exports.keys()) : []) out[`${name}.${m}`] = "member";
    } else if (sym.flags & (S.Function | S.Variable | S.ValueModule)) {
      const type = checker.getTypeOfSymbolAtLocation(sym, decl);
      const kind = sym.flags & S.Function ? "function" : sym.flags & S.ValueModule ? "namespace" : "const";
      out[name] = `${kind} ${clean(checker.typeToString(type, undefined, flags))}`;
    } else {
      out[name] = "export";
    }
  }
});
process.stdout.write(JSON.stringify(out));
process.exit(failed ? 4 : 0);
