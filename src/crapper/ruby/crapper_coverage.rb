# Loaded by crapper through RUBYOPT=-r. Records line and branch coverage with
# Ruby's built-in Coverage and writes a SimpleCov-shaped resultset at exit.
#
# Every Ruby process writes its own file, so `rake test` spawning a child, or
# a forked worker, does not overwrite another process's results. crapper
# merges them. No gem is needed and the project's Gemfile is left alone.
#
#   CRAPPER_RUBY_COVERAGE_DIR   where to write .resultset-<pid>.json
#   CRAPPER_RUBY_COVERAGE_ROOT  only files under this directory are kept
#
# This file loads before Coverage starts, so it cannot measure itself. Each
# method stays at one decision so its CRAP stays low at 0%. tests/test_ruby.py
# runs it under a real Ruby.
require "coverage"
require "fileutils"
require "json"

module CrapperCoverage
  SKIPPED = %r{/(?:spec|test|tests|vendor|\.bundle|node_modules)/}.freeze

  def self.start
    return if Coverage.running?

    Coverage.start(lines: true, branches: true)
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

  def self.files(root)
    Coverage.result.each_with_object({}) do |(path, data), out|
      next unless keep?(path, root)

      out[path] = { "lines" => data[:lines], "branches" => branches(data.fetch(:branches, {})) }
    end
  end

  def self.write
    dir = ENV.fetch("CRAPPER_RUBY_COVERAGE_DIR", "")
    return if dir.empty?

    dump(dir, File.join(File.expand_path(ENV.fetch("CRAPPER_RUBY_COVERAGE_ROOT", Dir.pwd)), ""))
  end

  # A failure here must not change the test run's exit status.
  def self.dump(dir, root)
    result = { "crapper" => { "coverage" => files(root), "timestamp" => Time.now.to_i } }
    FileUtils.mkdir_p(dir)
    File.write(File.join(dir, ".resultset-#{Process.pid}.json"), JSON.generate(result))
  rescue StandardError => e
    warn "crapper coverage: #{e.class}: #{e.message}"
  end
end

CrapperCoverage.start
