# Loaded by crapper through RUBYOPT=-r. Records line and branch coverage with
# Ruby's built-in Coverage and writes a SimpleCov-shaped resultset at exit.
# Method call counts are kept too: a method's `def` line runs when its class
# loads, so lines alone cannot tell a never-called method from a called one.
#
# Every Ruby process writes its own file, so `rake test` spawning a child, or
# a forked worker, does not overwrite another process's results. crapper
# merges them. No gem is needed and the project's Gemfile is left alone.
#
# Ruby allows one Coverage session. A tool that starts its own (SimpleCov)
# joins this one, and a result it takes is kept for crapper too, so the
# project's coverage setup and crapper's work side by side.
#
#   CRAPPER_RUBY_COVERAGE_DIR   where to write .resultset-<pid>.json
#   CRAPPER_RUBY_COVERAGE_ROOT  only files under this directory are kept
#
# This file loads before Coverage starts, so it cannot measure itself. Each
# method stays at one decision so its CRAP stays low at 0%. tests/test_ruby.py
# runs it under a real Ruby.
#
# Two things keep Bundler from getting ahead of it:
# - Nothing but Coverage is required up front. `require "json"` here would
#   activate the default json gem before Bundler.setup, and a Gemfile.lock
#   pinning another json fails with "You have already activated json".
# - Bundler (2.2+) exports BUNDLER_SETUP, and RubyGems requires it while it
#   boots, before any RUBYOPT -r. bundler/setup loads the gemspec, which
#   usually requires lib/<gem>/version.rb, so that file would load before
#   Coverage starts and never be measured. Right before Bundler's exec, and
#   after this file requires Bundler's setup, BUNDLER_SETUP is pointed here
#   and Bundler's own path kept in CRAPPER_BUNDLER_SETUP. RubyGems then loads
#   this file first, and this file requires Bundler's setup (see the end).
require "coverage"

module CrapperCoverage
  SKIPPED = %r{/(?:spec|test|tests|vendor|\.bundle|node_modules)/}.freeze
  CAPTURED = []
  BUNDLER_SETUP = "CRAPPER_BUNDLER_SETUP"

  # Prepended to Kernel's singleton class, which Bundler's exec calls.
  module AheadOfBundler
    def exec(*)
      CrapperCoverage.ahead_of_bundler
      super
    end
  end

  def self.ahead_of_bundler
    setup = ENV.fetch("BUNDLER_SETUP", __FILE__)
    return if setup == __FILE__

    ENV[BUNDLER_SETUP] = setup
    ENV["BUNDLER_SETUP"] = __FILE__
  end

  # Loaded as BUNDLER_SETUP: do what Bundler's file would have done. Only
  # then, so `Bundler.with_unbundled_env`, which drops BUNDLER_SETUP, stays
  # unbundled.
  def self.loaded_as_bundler_setup?
    ENV["BUNDLER_SETUP"] == __FILE__ && !defined?(Bundler)
  end

  # Prepended to Coverage's singleton class.
  module Shared
    def setup(*)
      return if running?

      super
    end

    def start(*)
      return if running?

      super
    end

    def result(*)
      super.tap { |found| CAPTURED << found }
    end
  end

  def self.start
    return if Coverage.running?

    Coverage.start(lines: true, branches: true, methods: true)
    Coverage.singleton_class.prepend(Shared)
    Kernel.singleton_class.prepend(AheadOfBundler)
    at_exit { CrapperCoverage.write }
  end

  def self.keep?(path, root)
    path.start_with?(root) && !SKIPPED.match?(path[root.length - 1..])
  end

  def self.branches(found)
    found.to_h do |condition, outcomes|
      [condition.inspect, outcomes.to_h { |outcome, count| [outcome.inspect, count] }]
    end
  end

  def self.methods_called(found)
    found.to_h { |method, calls| [method.inspect, calls] }
  end

  def self.files(root, result)
    result.each_with_object({}) do |(path, data), out|
      next unless keep?(path, root)

      out[path] = {
        "lines" => data[:lines],
        "branches" => branches(Hash(data[:branches])),
        "methods" => methods_called(Hash(data[:methods]))
      }
    end
  end

  # Results another tool took, then what is still running.
  def self.results
    Coverage.result if Coverage.running?
    CAPTURED
  end

  def self.write
    dir = ENV.fetch("CRAPPER_RUBY_COVERAGE_DIR", "")
    return if dir.empty?

    dump(dir, File.join(File.expand_path(ENV.fetch("CRAPPER_RUBY_COVERAGE_ROOT", Dir.pwd)), ""))
  end

  # A failure here must not change the test run's exit status. json loads
  # here, after Bundler chose its version. crapper created `dir`.
  def self.dump(dir, root)
    require "json"
    now = Time.now.to_i
    suites = results.each_with_index.to_h do |result, index|
      ["crapper-#{index}", { "coverage" => files(root, result), "timestamp" => now }]
    end
    File.write(File.join(dir, ".resultset-#{Process.pid}.json"), JSON.generate(suites))
  rescue StandardError => e
    warn "crapper coverage: #{e.class}: #{e.message}"
  end
end

CrapperCoverage.start
bundler_setup = ENV[CrapperCoverage::BUNDLER_SETUP]
require bundler_setup if bundler_setup && CrapperCoverage.loaded_as_bundler_setup?
# Bundler's setup exports its own path again; children start with this file.
CrapperCoverage.ahead_of_bundler
