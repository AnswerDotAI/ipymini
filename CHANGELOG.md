# Release notes

<!-- do not remove -->

## 0.1.14

### New Features

- Support `comm_manager`; replace per-subshell parent ContextVars and `comm_context` with shared module-level vars and `set_kernel` binding; stop echoing inbound comms on IOPub ([#37](https://github.com/AnswerDotAI/ipymini/issues/37))


## 0.1.13

### New Features

- Add SIGTERM handler ([#36](https://github.com/AnswerDotAI/ipymini/issues/36))


## 0.1.12

### New Features

- Shut down nested ipymini kernels when their launcher exits via `JPY_PARENT_PID` watcher ([#35](https://github.com/AnswerDotAI/ipymini/issues/35))


## 0.1.11

### New Features

- Add Concurrent execution helpers ([#34](https://github.com/AnswerDotAI/ipymini/issues/34))


## 0.1.10

### New Features

- Run router send and recv concurrently (kill ~40ms reply latency) ([#33](https://github.com/AnswerDotAI/ipymini/pull/33)), thanks to [@PiotrCzapla](https://github.com/PiotrCzapla)
- De-slop AsyncRouterThread.`run_async` ([#32](https://github.com/AnswerDotAI/ipymini/pull/32)), thanks to [@PiotrCzapla](https://github.com/PiotrCzapla)
- Propagate thread-local IO context through ThreadPoolExecutor.submit ([#27](https://github.com/AnswerDotAI/ipymini/issues/27))
- Add unlock() and subshell() opt-ins for concurrent in-cell execution ([#26](https://github.com/AnswerDotAI/ipymini/issues/26))


## 0.1.9

### New Features

- Add concurrent execution support via WorkTracker/ScopeGroup; isolate display hook per-context ([#25](https://github.com/AnswerDotAI/ipymini/issues/25))
- Interrupt handling: wake async scopes, add SIGUSR1 faulthandler dump, tighten subshell lifecycle ([#23](https://github.com/AnswerDotAI/ipymini/issues/23))


## 0.1.8

- Refactor using microio


## 0.1.7

### New Features

- Propagate contextvars to user-launched threads for correct output routing ([#22](https://github.com/AnswerDotAI/ipymini/issues/22))


## 0.1.6

### New Features

- Set kernel attr ([#21](https://github.com/AnswerDotAI/ipymini/issues/21))


## 0.1.5

### Bugs Squashed

- Fix debugger start state for gutter breakpoints ([#20](https://github.com/AnswerDotAI/ipymini/pull/20)), thanks to [@jph00](https://github.com/jph00)


## 0.1.4

### New Features

- Reduce LoC ([#18](https://github.com/AnswerDotAI/ipymini/pull/18)), thanks to [@jph00](https://github.com/jph00)
- Suppress planned shutdown warnings ([#17](https://github.com/AnswerDotAI/ipymini/pull/17)), thanks to [@jph00](https://github.com/jph00)
- async-zmq poller + review fixes ([#15](https://github.com/AnswerDotAI/ipymini/pull/15)), thanks to [@jph00](https://github.com/jph00)
- Delete PR branch after merge ([#14](https://github.com/AnswerDotAI/ipymini/pull/14)), thanks to [@jph00](https://github.com/jph00)
- Fix async eval + loop cleanup ([#12](https://github.com/AnswerDotAI/ipymini/pull/12)), thanks to [@jph00](https://github.com/jph00)
- Tighten exceptions and debug deps ([#11](https://github.com/AnswerDotAI/ipymini/pull/11)), thanks to [@jph00](https://github.com/jph00)
- Use iopub proxy everywhere ([#10](https://github.com/AnswerDotAI/ipymini/pull/10)), thanks to [@jph00](https://github.com/jph00)
- Rename pytests to tests and streamline iopub send ([#9](https://github.com/AnswerDotAI/ipymini/pull/9)), thanks to [@jph00](https://github.com/jph00)
- Refine timeout helpers and tests ([#5](https://github.com/AnswerDotAI/ipymini/pull/5)), thanks to [@jph00](https://github.com/jph00)
- Improve kernel interrupt handling and tests ([#4](https://github.com/AnswerDotAI/ipymini/pull/4)), thanks to [@jph00](https://github.com/jph00)
- Respect display_page pager config ([#3](https://github.com/AnswerDotAI/ipymini/pull/3)), thanks to [@jph00](https://github.com/jph00)
### Bugs Squashed

- fix `display_page` ([#19](https://github.com/AnswerDotAI/ipymini/issues/19))
- Harden router/iopub delivery and expand kernel tests ([#16](https://github.com/AnswerDotAI/ipymini/pull/16)), thanks to [@jph00](https://github.com/jph00)
- Fix interrupt handling and main-thread execution ([#2](https://github.com/AnswerDotAI/ipymini/pull/2)), thanks to [@jph00](https://github.com/jph00)
