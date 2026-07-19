export class QueueFullError extends Error {
  constructor() {
    super('queue is full');
    this.name = 'QueueFullError';
  }
}

export class QueueClosedError extends Error {
  constructor() {
    super('queue is closed');
    this.name = 'QueueClosedError';
  }
}

/** A small in-memory FIFO with an explicit bound on waiting work. */
export class BoundedQueue {
  constructor(concurrency, maxQueued) {
    if (!Number.isInteger(concurrency) || concurrency < 1)
      throw new TypeError('concurrency must be a positive integer');
    if (!Number.isInteger(maxQueued) || maxQueued < 0)
      throw new TypeError('maxQueued must be a non-negative integer');
    this.concurrency = concurrency;
    this.maxQueued = maxQueued;
    this.active = 0;
    this.waiters = [];
    this.closed = false;
  }

  run(fn) {
    if (this.closed) return Promise.reject(new QueueClosedError());
    if (this.active < this.concurrency) return this.#start(fn);
    if (this.waiters.length >= this.maxQueued) return Promise.reject(new QueueFullError());
    return new Promise((resolve, reject) => this.waiters.push({ fn, resolve, reject }));
  }

  #start(fn) {
    this.active++;
    return Promise.resolve()
      .then(fn)
      .finally(() => {
        this.active--;
        const next = this.waiters.shift();
        if (next) this.#start(next.fn).then(next.resolve, next.reject);
      });
  }

  close() {
    this.closed = true;
    for (const waiter of this.waiters.splice(0)) waiter.reject(new QueueClosedError());
  }

  snapshot() {
    return {
      active: this.active,
      queued: this.waiters.length,
      concurrency: this.concurrency,
      maxQueued: this.maxQueued,
      closed: this.closed,
    };
  }
}
