import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crapper.coverage import CoverageBundle, load_bundle, parse_simplecov
from crapper.discover import is_test_file, iter_source_files, language_of
from crapper.languages.ruby import functions_in_source
from crapper.model import Function
from crapper.runners import (
    _RUBY_SHIM,
    _clear_resultsets,
    _cover_ruby,
    ruby_coverage_env,
    ruby_roots,
    ruby_test_command,
    run_coverage,
)


def _rows(source, path="lib/demo/box.rb", root="/proj"):
    return [
        (fn.namespace, fn.name, fn.complexity)
        for fn in functions_in_source(source, path, root)
    ]


def test_each_decision_counts_once():
    source = """
module Demo
  class Box
    def choose(x, ready)
      if x > 0 && x < 10
        1
      elsif x == 0 || ready
        0
      else
        -1
      end
      unless ready then 1 end
      while ready do break end
      until ready do break end
      for i in xs do i end
      begin
        x
      rescue ArgumentError
        0
      rescue KeyError, IndexError => e
        1
      ensure
        2
      end
      case x
      when 1, 2 then :low
      when 3 then :mid
      else :high
      end
      case [x]
      in [Integer => n] if n > 0 then n
      in [String] unless ready then 0
      in _ then 1
      end
      y = ready ? 1 : 0
      y = 2 if ready and x
      y = 3 unless ready or x
      y += 1 while y < 3
      y -= 1 until y < 0
      z = risky rescue nil
      w = x&.to_s
      @memo ||= {}
      @flag &&= ready
      y += 1
      not ready
    end
  end
end
"""
    # 1 + 28: if, &&, elsif, ||, unless, while, until, for, 2 rescue,
    # 2 when, 3 in, if guard, unless guard, ternary, if mod, and, unless mod,
    # or, while mod, until mod, rescue mod, &., ||=, &&=
    assert _rows(source) == [("Demo::Box", "choose", 29)]


def test_strings_comments_and_heredocs_do_not_add_decisions():
    source = '''
def literal
  # if x && y
  text = "if x || y"
  more = <<~TEXT
    unless a then b end
  TEXT
  %w[if and or]
end
'''
    assert _rows(source) == [("demo/box", "literal", 1)]


def test_names_and_namespaces():
    source = """
module Demo
  class Box < Base
    def open(flag) = flag ? 1 : 0
    def self.build(x)
      x
    end
    def ==(other); end
    def name=(value); end
    def [](index); end
    def ready?; true; end
    private def secret; end

    class << self
      def meta
        1 if true
      end
    end

    class Lid
      def close; end
    end
  end

  module Helpers
    def self.util; end
    def assist; end
  end
end

class Demo::Inner::Thing
  def go; end
end

class ::Root
  def top; end
end

def obj.only_here; end

def toplevel(x)
  def inner(y)
    y if y
  end
  [1].each { |i| i if i }
  -> { x if x }
end
"""
    assert _rows(source) == [
        ("Demo::Box", "open", 2),
        ("Demo::Box", "self.build", 1),
        ("Demo::Box", "==", 1),
        ("Demo::Box", "name=", 1),
        ("Demo::Box", "[]", 1),
        ("Demo::Box", "ready?", 1),
        ("Demo::Box", "secret", 1),
        ("Demo::Box", "self.meta", 2),
        ("Demo::Box::Lid", "close", 1),
        ("Demo::Helpers", "self.util", 1),
        ("Demo::Helpers", "assist", 1),
        ("Demo::Inner::Thing", "go", 1),
        ("Root", "top", 1),
        ("demo/box", "obj.only_here", 1),
        ("demo/box", "toplevel", 4),
    ]


def test_methods_inside_blocks_keep_the_module_namespace():
    source = """
module Trackable
  extend ActiveSupport::Concern
  included do
    def track(event)
      log(event) if event
    end
  end
end
"""
    assert _rows(source) == [("Trackable", "track", 2)]


def test_file_namespace_strips_root_lib_src_and_suffix():
    source = "def run\n  1\nend\n"
    assert functions_in_source(source, "/proj/lib/demo/app.rb", "/proj")[0].namespace == "demo/app"
    assert functions_in_source(source, "./src/demo/app.rb", ".")[0].namespace == "demo/app"
    assert functions_in_source(source, "gems/core/lib/core.rb", None)[0].namespace == "core"
    assert functions_in_source(source, "app/models/user.rb", "/proj")[0].namespace == "app/models/user"


def test_lines_and_language():
    source = "class A\n  def run\n    1\n  end\nend\n"
    function = functions_in_source(source, "lib/a.rb", "/proj")[0]
    assert (function.start_line, function.end_line, function.language) == (2, 4, "ruby")


def test_discovery_skips_ruby_tests(tmp_path):
    for name in ("lib/box.rb", "lib/box_spec.rb", "lib/box_test.rb", "lib/test_box.rb",
                 "spec/helper.rb", ".bundle/ruby/gem.rb", "Rakefile"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def x; end\n")
    assert language_of("lib/box.rb") == "ruby"
    assert is_test_file("lib/box_spec.rb") and is_test_file("lib/test_box.rb")
    assert not is_test_file("lib/testing.rb")
    assert iter_source_files([tmp_path]) == [(tmp_path / "lib" / "box.rb").resolve()]


def _resultset(path, lines, branches=None, suite="RSpec"):
    entry = {"lines": lines}
    if branches is not None:
        entry["branches"] = branches
    return json.dumps({suite: {"coverage": {path: entry}, "timestamp": 1}})


def test_simplecov_lines_and_branches():
    text = _resultset(
        "/proj/lib/box.rb",
        [None, 1, 0, 2, None],
        {
            "[:if, 0, 2, 4, 4, 7]": {"[:then, 1, 3, 6, 3, 7]": 0, "[:else, 2, 4, 6, 4, 7]": 2},
            '[:"&.", 3, 4, 2, 4, 9]': {"[:then, 4, 4, 2, 4, 9]": 1, "[:else, 5, 4, 2, 4, 9]": 0},
            "not a key": {"x": 1},
            "[:case, 6, 9, 0, 9, 1]": "broken",
        },
    )
    record = parse_simplecov([text])["/proj/lib/box.rb"]
    assert dict(record) == {2: (1, 1), 3: (0, 1), 4: (1, 1)}
    assert record.branches == {2: (1, 2), 4: (1, 2)}


def test_simplecov_merges_suites_and_processes():
    first = _resultset("/p/a.rb", [1, 0, None], {"[:if, 0, 1, 0, 2, 3]": {"[:then, 1, 1, 0, 1, 1]": 0}})
    second = _resultset(
        "/p/a.rb", [0, 3, None], {"[:if, 0, 1, 0, 2, 3]": {"[:then, 1, 1, 0, 1, 1]": 4}}, "Minitest"
    )
    record = parse_simplecov([first, second, "not json", "[]", json.dumps({"x": 1, "y": {}})])["/p/a.rb"]
    assert dict(record) == {1: (1, 1), 2: (1, 1)}
    assert record.branches == {1: (1, 1)}


def test_simplecov_legacy_line_arrays():
    text = json.dumps({"RSpec": {"coverage": {"/p/a.rb": [1, 0, None, "x"]}}})
    record = parse_simplecov([text])["/p/a.rb"]
    assert dict(record) == {1: (1, 1), 2: (0, 1)}
    assert record.branches == {}
    assert dict(parse_simplecov([json.dumps({"S": {"coverage": {"/p/b.rb": 7}}})])["/p/b.rb"]) == {}


def test_bundle_reads_shim_and_simplecov_resultsets(tmp_path):
    shim = tmp_path / "target" / "coverage" / "ruby" / "gems__core"
    shim.mkdir(parents=True)
    (shim / ".resultset-12.json").write_text(
        _resultset(str(tmp_path / "gems/core/lib/core.rb"), [None, 1, None])
    )
    own = tmp_path / "coverage"
    own.mkdir()
    (own / ".resultset.json").write_text(_resultset(str(tmp_path / "lib/box.rb"), [None, 1, 0, None]))
    bundle = load_bundle(tmp_path)
    box = Function("open", "Box", 1, 2, 3, "lib/box.rb", "ruby")
    core = Function("run", "Core", 1, 1, 3, "gems/core/lib/core.rb", "ruby")
    assert bundle.percent_for(box) == 50.0
    assert bundle.percent_for(core) == 100.0


def test_lcov_for_the_same_file_wins_over_simplecov(tmp_path):
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage" / ".resultset.json").write_text(_resultset("lib/a.rb", [0]))
    (tmp_path / "coverage" / "lcov.info").write_text("SF:lib/a.rb\nDA:1,1\nend_of_record\n")
    bundle = load_bundle(tmp_path)
    assert bundle.percent_for(Function("a", "A", 1, 1, 1, "lib/a.rb", "ruby")) == 100.0


def _touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_ruby_roots_use_the_nearest_gemfile(tmp_path):
    _touch(tmp_path / "gems" / "core" / "Gemfile")
    nested = _touch(tmp_path / "gems" / "core" / "lib" / "core.rb")
    loose = _touch(tmp_path / "lib" / "app.rb")
    other = _touch(tmp_path / "web" / "app.py")
    assert ruby_roots(tmp_path, [nested, loose, other]) == [
        tmp_path.resolve(),
        (tmp_path / "gems" / "core").resolve(),
    ]


def test_ruby_test_command_picks_the_runner(tmp_path):
    assert ruby_test_command(tmp_path) is None
    (tmp_path / "test").mkdir()
    loader = ruby_test_command(tmp_path)
    assert loader[:3] == ["ruby", "-Ilib", "-Itest"] and "test/**/{*_test,test_*}.rb" in loader[-1]
    _touch(tmp_path / "Rakefile")
    assert ruby_test_command(tmp_path) == ["rake", "test"]
    _touch(tmp_path / ".rspec")
    assert ruby_test_command(tmp_path) == ["rspec"]
    _touch(tmp_path / "Gemfile")
    assert ruby_test_command(tmp_path) == ["bundle", "exec", "rspec"]


def test_coverage_env_appends_the_shim(tmp_path):
    env = ruby_coverage_env(tmp_path, tmp_path / "out", {"RUBYOPT": "-W0", "PATH": "/bin"})
    assert env["RUBYOPT"] == f"-W0 -r{_RUBY_SHIM}"
    assert env["CRAPPER_RUBY_COVERAGE_DIR"] == str(tmp_path / "out")
    assert env["CRAPPER_RUBY_COVERAGE_ROOT"] == str(tmp_path)
    assert env["PATH"] == "/bin"
    assert ruby_coverage_env(tmp_path, tmp_path, {})["RUBYOPT"] == f"-r{_RUBY_SHIM}"


def test_a_shim_path_with_a_space_is_copied(tmp_path, monkeypatch):
    spaced = _touch(tmp_path / "with space" / "crapper_coverage.rb", "# shim\n")
    monkeypatch.setattr("crapper.runners._RUBY_SHIM", spaced)
    option = ruby_coverage_env(tmp_path, tmp_path, {})["RUBYOPT"]
    copy = Path(option[len("-r"):])
    assert " " not in str(copy) and copy.read_text() == "# shim\n"


def test_clear_resultsets_keeps_nested_packages(tmp_path):
    _clear_resultsets(tmp_path / "missing")
    own = _touch(tmp_path / ".resultset-1.json")
    nested = _touch(tmp_path / "gems__core" / ".resultset-2.json")
    other = _touch(tmp_path / "notes.txt")
    _clear_resultsets(tmp_path)
    assert not own.exists() and nested.exists() and other.exists()


def test_cover_ruby_always_runs_with_the_shim(tmp_path, monkeypatch, capsys):
    calls = []

    def fake(command, cwd, env=None):
        calls.append((command, cwd, env))
        return 3

    monkeypatch.setattr("crapper.runners.run_shell", fake)
    source = _touch(tmp_path / "lib" / "box.rb")
    _touch(tmp_path / ".rspec")
    stale = _touch(tmp_path / "target" / "coverage" / "ruby" / ".resultset-9.json")
    run_coverage(tmp_path, [source], None)
    command, cwd, env = calls[-1]
    assert command == ["rspec"] and cwd == tmp_path.resolve()
    assert env["CRAPPER_RUBY_COVERAGE_DIR"] == str(stale.parent)
    assert not stale.exists()
    assert "Ruby tests exited 3" in capsys.readouterr().err

    # A Gemfile naming SimpleCov does not mean this run starts it (RuboCop
    # only does under COVERAGE=1 rake), so the shim still runs, and a
    # leftover SimpleCov resultset is removed.
    _touch(tmp_path / "Gemfile.lock", "    simplecov (1.2.0)\n")
    old = _touch(tmp_path / "coverage" / ".resultset.json")
    _cover_ruby(tmp_path, [source])
    assert calls[-1][2]["RUBYOPT"].endswith(f"-r{_RUBY_SHIM}")
    assert not old.exists()


def test_cover_ruby_without_tests_is_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("crapper.runners.run_shell", lambda *_a, **_k: pytest.fail("ran"))
    _cover_ruby(tmp_path, [_touch(tmp_path / "lib" / "box.rb")])
    assert "skipping Ruby coverage" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_shim_records_lines_and_branches_from_a_real_ruby(tmp_path):
    lib = _touch(
        tmp_path / "lib" / "box.rb",
        "def pick(x)\n  if x\n    1\n  else\n    0\n  end\nend\n",
    )
    _touch(tmp_path / "test" / "box_test.rb", "require 'box'\npick(true)\n")
    _touch(tmp_path / "vendor" / "skip.rb", "1\n")
    out = tmp_path / "out"
    out.mkdir()
    env = ruby_coverage_env(tmp_path, out)
    script = "require 'box'; require_relative 'vendor/skip'; pick(true); fork { pick(false) }; Process.wait"
    subprocess.run(["ruby", "-Ilib", "-e", script], cwd=tmp_path, env=env, check=True)
    texts = [path.read_text() for path in sorted(out.glob(".resultset-*.json"))]
    assert len(texts) == 2
    coverage = parse_simplecov(texts)
    assert list(coverage) == [str(lib.resolve())]
    record = coverage[str(lib.resolve())]
    assert record.branches == {2: (2, 2)}
    assert dict(record) == {1: (1, 1), 2: (1, 1), 3: (1, 1), 5: (1, 1)}


def test_a_constant_assigned_a_block_names_its_methods():
    source = """
module Adapters
  Key = Struct.new(:from, :options) do
    def name
      options[:name] if options
    end
  end
  self::Point = Data.define(:x) { def norm = x }
  Klass = Class.new(Base) do
    class << self
      def build; end
    end
  end
  HANDLERS = Hash.new { |h, k| h[k] = [] }
  handler = proc do
    def loose; end
  end
end
"""
    assert _rows(source) == [
        ("Adapters::Key", "name", 2),
        ("Adapters::Point", "norm", 1),
        ("Adapters::Klass", "self.build", 1),
        ("Adapters", "loose", 1),
    ]


def test_a_monorepo_runs_each_gemspec_component(tmp_path):
    _touch(tmp_path / "Gemfile", "gemspec path: 'support'\n")
    _touch(tmp_path / "Gemfile.lock", "    rake (13.0)\n")
    support = tmp_path / "support"
    _touch(support / "support.gemspec")
    _touch(support / "Rakefile")
    source = _touch(support / "lib" / "support" / "core.rb")
    tool = _touch(tmp_path / "tools" / "release.rb")
    assert ruby_roots(tmp_path, [source, tool]) == [tmp_path.resolve(), support.resolve()]
    assert ruby_test_command(support, tmp_path) == ["bundle", "exec", "rake", "test"]
    assert ruby_test_command(support) == ["rake", "test"]


def test_a_custom_command_records_ruby_coverage(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "crapper.runners.run_shell",
        lambda command, cwd, env=None: calls.append((command, env)) or 0,
    )
    stale = _touch(tmp_path / "target" / "coverage" / "ruby" / ".resultset-1.json")
    nested = _touch(tmp_path / "target" / "coverage" / "ruby" / "alpha" / ".resultset-2.json")
    assert run_coverage(tmp_path, [_touch(tmp_path / "lib" / "a.rb")], "cd sub && bin/test") == 0
    command, env = calls[-1]
    assert command == "cd sub && bin/test"
    assert env["CRAPPER_RUBY_COVERAGE_ROOT"] == str(tmp_path.resolve())
    assert not stale.exists() and not nested.exists()
    run_coverage(tmp_path, [_touch(tmp_path / "web" / "a.py")], "pytest")
    assert calls[-1] == ("pytest", None)


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_shim_shares_coverage_with_a_tool_that_takes_the_result(tmp_path):
    """SimpleCov starts Coverage again and takes the result at exit. Both must work."""

    lib = _touch(tmp_path / "lib" / "box.rb", "def pick(x)\n  x ? 1 : 0\nend\n")
    script = (
        "Coverage.start(lines: true)\n"
        "at_exit { $stdout.puts Coverage.result.keys.grep(/box/).size }\n"
        "require 'box'\n"
        "pick(true)\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    done = subprocess.run(
        ["ruby", "-Ilib", "-e", script],
        cwd=tmp_path, env=ruby_coverage_env(tmp_path, out), capture_output=True, text=True,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "1\n", "")
    texts = [path.read_text() for path in out.glob(".resultset-*.json")]
    record = parse_simplecov(texts)[str(lib.resolve())]
    assert dict(record) == {1: (1, 1), 2: (1, 1)}
    assert record.branches == {2: (1, 2)}


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_shim_starts_before_a_bundler_setup_that_rubygems_loads(tmp_path):
    """Bundler exports BUNDLER_SETUP and RubyGems requires it while booting,
    before RUBYOPT. A stand-in setup loads the gem's version file, as
    bundler/setup does through the gemspec, then exports itself again."""

    version = _touch(tmp_path / "lib" / "gem_version.rb", "def gem_version(x)\n  x ? 1 : 0\nend\n")
    setup = _touch(
        tmp_path / "fake_setup.rb",
        "module Bundler; end\n"
        f"ENV['BUNDLER_SETUP'] = {str(tmp_path / 'fake_setup.rb')!r}\n"
        "require_relative 'lib/gem_version'\n",
    )
    out = tmp_path / "out"
    out.mkdir()
    env = ruby_coverage_env(tmp_path, out)
    # `bundle exec` sets BUNDLER_SETUP, then Kernel.exec runs the command.
    parent = (
        f"ENV['BUNDLER_SETUP'] = {str(setup)!r}\n"
        "Kernel.exec('ruby', '-e', 'gem_version(true); "
        "system(%q(ruby), %q(-e), %q(print ENV[%q(BUNDLER_SETUP)] == $LOADED_FEATURES.grep(/crapper_coverage/).first))')"
    )
    done = subprocess.run(["ruby", "-e", parent], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == "true", "a grandchild also boots with the shim as BUNDLER_SETUP"
    record = parse_simplecov([path.read_text() for path in out.glob(".resultset-*.json")])[str(version.resolve())]
    assert dict(record) == {1: (1, 1), 2: (1, 1)}
    assert record.branches == {2: (1, 2)}


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_shim_does_not_load_json_before_bundler(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    done = subprocess.run(
        ["ruby", "-e", "print defined?(JSON).inspect, ' ', defined?(FileUtils).inspect"],
        cwd=tmp_path, env=ruby_coverage_env(tmp_path, out), capture_output=True, text=True,
    )
    assert done.stdout == "nil nil"
    assert len(list(out.glob(".resultset-*.json"))) == 1


def test_a_never_called_ruby_method_is_zero_even_though_its_def_line_ran(tmp_path):
    path = str(tmp_path / "lib" / "a.rb")
    lines = [1, 1, 0, 0, None, None, 1, 1, None, None]
    methods = {
        "[A, :never, 2, 2, 5, 5]": 0,
        "[A, :once, 7, 2, 9, 5]": 1,
        "[#<Class:A>, :build, 7, 2, 9, 5]": 0,
        "not a method": 1,
        "[A, :odd, 1, 0, 1, 9]": "x",
    }
    first = _resultset(path, lines)
    second = json.dumps({"S": {"coverage": {path: {"lines": lines, "methods": methods}}}})
    record = parse_simplecov([first, second])[path]
    assert record.methods == {2: False, 7: True, 1: False}
    bundle = CoverageBundle(lcov={path: record})
    never = Function("never", "A", 1, 2, 5, path, "ruby")
    once = Function("once", "A", 1, 7, 9, path, "ruby")
    unrecorded = Function("other", "A", 1, 10, 10, path, "ruby")
    assert bundle.percent_for(never) == 0.0
    assert bundle.percent_for(once) == 100.0
    assert bundle.percent_for(unrecorded) == 0.0


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_shim_scores_a_never_called_branchless_method_at_zero(tmp_path):
    source = "class A\n  def never(x)\n    y = x + 1\n    y * 2\n  end\n\n  def once(x)\n    x + 1\n  end\nend\n"
    lib = _touch(tmp_path / "lib" / "a.rb", source)
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        ["ruby", "-Ilib", "-e", "require 'a'; A.new.once(1)"],
        cwd=tmp_path, env=ruby_coverage_env(tmp_path, out), check=True,
    )
    record = parse_simplecov([p.read_text() for p in out.glob(".resultset-*.json")])
    bundle = CoverageBundle(lcov=record)
    scores = {
        fn.name: bundle.percent_for(fn)
        for fn in functions_in_source(source, str(lib.resolve()), str(tmp_path))
    }
    assert scores == {"never": 0.0, "once": 100.0}
