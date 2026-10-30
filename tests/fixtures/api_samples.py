"""Sample sources for the Go, Rust and Java API checkers."""

GO = {
    "store/store.go": '''package store

import "fmt"

// Store holds records.
type Store struct {
	Name    string
	Size, Cap int
	secret  string
	*Base
	Tags    []string `json:"tags"`
}

type Reader interface {
	Read(key string) ([]byte, error)
	io.Closer
	hidden()
}

type ID = string

const (
	MaxSize = 10
	minSize = 1
)

var Default = New("x")

func New(name string, opts ...Option) *Store {
	return &Store{Name: name} // { not a brace
}

func (s *Store) Get(key, fallback string) (string, error) {
	s := "}"
	return fmt.Sprint(s), nil
}

func (s *Store) put() {}

func helper() {}
''',
    "store/store_test.go": "package store\n\nfunc TestX(t *testing.T) {}\n",
    "internal/x/x.go": "package x\n\nfunc Exported() {}\n",
}

RUST = {
    "src/lib.rs": '''//! Crate docs
pub mod parse;

/// A document.
#[derive(Debug, Clone)]
pub struct Doc {
    pub text: String,
    pub(crate) cache: Vec<u8>,
    len: usize,
}

pub enum Mode {
    Lenient,
    Strict { level: u8 },
    Custom(String),
}

pub trait Render {
    fn render(&self, width: usize) -> String;
    fn name(&self) -> &str { "x" }
}

impl Doc {
    pub fn new(text: &str) -> Self { Doc { text: text.into(), cache: vec![], len: 0 } }
    pub fn len(&self) -> usize { self.len }
    fn private(&self) {}
}

impl Render for Doc {
    fn render(&self, width: usize) -> String { "}".into() }
}

pub const VERSION: &str = "1";
pub(crate) fn internal() {}
pub fn parse_doc<'a>(input: &'a str, mode: Mode) -> Result<Doc, String> { todo!() }
pub use parse::Parser;
''',
    "src/parse.rs": "pub struct Parser;\n\nimpl Parser {\n    pub async fn run(&mut self, src: &str) -> bool { true }\n}\n",
}

JAVA = {
    "src/main/java/com/acme/Client.java": '''package com.acme;

import java.util.List;

/** A client. */
public class Client implements AutoCloseable {
    public static final int TIMEOUT = 30;
    protected String url;
    private int retries;

    public Client(String url) { this.url = url; }

    public String get(String path) throws java.io.IOException { return "}"; }

    public String get(String path, int timeoutMs) { return ""; }

    @Override
    public void close() {}

    void packagePrivate() {}

    protected <T> List<T> all(Class<T> type) { return null; }
}
''',
    "src/main/java/com/acme/Api.java": '''package com.acme;

public interface Api {
    String fetch(String id);
    default int version() { return 1; }
}
''',
    "src/main/java/com/acme/Mode.java": "package com.acme;\n\npublic enum Mode { LENIENT, STRICT; public boolean strict() { return this == STRICT; } }\n",
    "src/main/java/com/acme/Hidden.java": "package com.acme;\n\nclass Hidden { public void x() {} }\n",
}
