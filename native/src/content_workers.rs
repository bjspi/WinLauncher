//! Bounded content scans; results are consumed in candidate order to preserve ties.
//!
//! A worker can block for a long time on an unreachable network path. Cancelling
//! therefore never waits for workers: pending results are abandoned and busy
//! workers are left to finish their current file and exit on their own.

use std::collections::VecDeque;
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::content::ContentFilter;
use crate::heap::Kept;
use crate::CancelFlag;

/// Scans queued per worker, so workers stay busy while results are consumed in order.
const PENDING_SCANS_PER_WORKER: usize = 2;
/// How often a waiting search checks for cancellation.
const CANCEL_POLL: Duration = Duration::from_millis(20);

type Job = (Kept, mpsc::Sender<Option<Kept>>);

pub(crate) struct ContentWorkers {
    jobs: Option<mpsc::SyncSender<Job>>,
    pending: VecDeque<mpsc::Receiver<Option<Kept>>>,
    workers: Vec<JoinHandle<()>>,
    stop: CancelFlag,
    cancel: CancelFlag,
    capacity: usize,
}

impl ContentWorkers {
    pub fn new(filter: ContentFilter, threads: usize, cancel: &CancelFlag) -> PyResult<Self> {
        let capacity = threads * PENDING_SCANS_PER_WORKER;
        let (sender, receiver) = mpsc::sync_channel::<Job>(capacity);
        let receiver = Arc::new(Mutex::new(receiver));
        let mut pool = Self {
            jobs: Some(sender),
            pending: VecDeque::new(),
            workers: Vec::new(),
            stop: CancelFlag::default(),
            cancel: cancel.clone(),
            capacity,
        };
        for index in 0..threads {
            let worker = spawn_worker(
                index,
                Arc::clone(&receiver),
                filter.clone(),
                cancel.clone(),
                pool.stop.clone(),
            )?;
            pool.workers.push(worker);
        }
        Ok(pool)
    }

    /// Queue a scan; returns the oldest finished hit when the queue was full.
    pub fn submit(&mut self, hit: Kept) -> PyResult<Option<Kept>> {
        let ready = if self.pending.len() == self.capacity {
            self.next()?
        } else {
            None
        };
        if self.cancel.is_set() {
            return Ok(None); // Never block on a full queue after cancellation.
        }
        let (sender, receiver) = mpsc::channel();
        let jobs = self.jobs.as_ref().expect("content queue is open");
        jobs.send((hit, sender))
            .map_err(|_| PyRuntimeError::new_err("Content workers stopped unexpectedly"))?;
        self.pending.push_back(receiver);
        Ok(ready)
    }

    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    /// The oldest scan's hit (None if it did not match), or None once cancelled.
    pub fn next(&mut self) -> PyResult<Option<Kept>> {
        let Some(receiver) = self.pending.pop_front() else {
            return Ok(None);
        };
        loop {
            if self.cancel.is_set() {
                return Ok(None);
            }
            match receiver.recv_timeout(CANCEL_POLL) {
                Ok(result) => return Ok(result),
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(PyRuntimeError::new_err(
                        "Content worker stopped unexpectedly",
                    ))
                }
            }
        }
    }
}

fn spawn_worker(
    index: usize,
    jobs: Arc<Mutex<mpsc::Receiver<Job>>>,
    mut filter: ContentFilter,
    cancel: CancelFlag,
    stop: CancelFlag,
) -> PyResult<JoinHandle<()>> {
    thread::Builder::new()
        .name(format!("winlauncher-content-{index}"))
        .spawn(move || {
            while !stop.is_set() {
                let job = jobs.lock().expect("content queue lock").recv();
                let Ok((mut hit, result)) = job else { break };
                let found = !stop.is_set() && filter.matches(&hit.row, &cancel);
                if found {
                    hit.ranking.reasons.push("Content contains query".into());
                }
                let _ = result.send(found.then_some(hit));
            }
        })
        .map_err(|error| PyRuntimeError::new_err(format!("Content worker: {error}")))
}

impl Drop for ContentWorkers {
    fn drop(&mut self) {
        self.stop.set();
        self.jobs.take();
        if self.cancel.is_set() {
            // Busy workers may be stuck on a network path; let them exit later.
            self.workers.clear();
            return;
        }
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}
