//! Bounded content scans; results are consumed in candidate order to preserve ties.

use std::collections::VecDeque;
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::content::ContentFilter;
use crate::heap::Kept;
use crate::CancelFlag;

type Job = (Kept, mpsc::Sender<Option<Kept>>);

pub(crate) struct ContentWorkers {
    jobs: Option<mpsc::SyncSender<Job>>,
    pending: VecDeque<mpsc::Receiver<Option<Kept>>>,
    workers: Vec<JoinHandle<()>>,
    stop: CancelFlag,
    capacity: usize,
}

impl ContentWorkers {
    pub fn new(filter: ContentFilter, threads: usize, cancel: &CancelFlag) -> PyResult<Self> {
        let capacity = threads * 2;
        let (sender, receiver) = mpsc::sync_channel::<Job>(capacity);
        let receiver = Arc::new(Mutex::new(receiver));
        let mut pool = Self {
            jobs: Some(sender),
            pending: VecDeque::new(),
            workers: Vec::new(),
            stop: CancelFlag::default(),
            capacity,
        };
        for index in 0..threads {
            let jobs = Arc::clone(&receiver);
            let cancel = cancel.clone();
            let stop = pool.stop.clone();
            let mut filter = filter.clone();
            let worker = thread::Builder::new()
                .name(format!("pathscout-content-{index}"))
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
                .map_err(|error| PyRuntimeError::new_err(format!("Content worker: {error}")))?;
            pool.workers.push(worker);
        }
        Ok(pool)
    }

    pub fn submit(&mut self, hit: Kept) -> PyResult<Option<Kept>> {
        let ready = if self.pending.len() == self.capacity {
            self.next()?
        } else {
            None
        };
        let (sender, receiver) = mpsc::channel();
        self.jobs
            .as_ref()
            .expect("content queue is open")
            .send((hit, sender))
            .map_err(|_| PyRuntimeError::new_err("Content workers stopped unexpectedly"))?;
        self.pending.push_back(receiver);
        Ok(ready)
    }

    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    pub fn next(&mut self) -> PyResult<Option<Kept>> {
        self.pending
            .pop_front()
            .expect("pending content scan")
            .recv()
            .map_err(|_| PyRuntimeError::new_err("Content worker stopped unexpectedly"))
    }
}

impl Drop for ContentWorkers {
    fn drop(&mut self) {
        self.stop.set();
        self.jobs.take();
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}
