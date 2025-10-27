use pyo3::prelude::*;
use redis::{Commands, PipelineCommands};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[pyclass]
struct Store {
    url: String,
}

#[pymethods]
impl Store {
    #[new]
    fn new(url: Option<String>) -> PyResult<Self> {
        Ok(Self { url: url.unwrap_or_else(|| "redis://127.0.0.1:6379/0".to_string()) })
    }

    fn ping(&self) -> PyResult<bool> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        let pong: String = redis::cmd("PING").query(&mut conn).map_err(to_py)?;
        Ok(pong == "PONG")
    }

    fn set_last_block(&self, blk: u64) -> PyResult<()> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        redis::cmd("SET").arg("progress:last_block").arg(blk).query::<()>(&mut conn).map_err(to_py)?;
        Ok(())
    }

    fn get_last_block(&self) -> PyResult<Option<u64>> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        let res: Option<u64> = redis::cmd("GET").arg("progress:last_block").query(&mut conn).map_err(to_py)?;
        Ok(res)
    }

    fn upsert_vault_meta(&self, meta: HashMap<String, (String, String)>) -> PyResult<()> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        let mut pipe = redis::pipe();
        pipe.atomic();
        for (v, (q, b)) in meta {
            let obj = serde_json::json!({"quote": q.to_lowercase(), "base": b.to_lowercase(), "updated_at": now_ts()});
            pipe.hset("vault:meta", v.to_lowercase(), obj.to_string());
        }
        pipe.query(&mut conn).map_err(to_py)?;
        Ok(())
    }

    fn load_vault_meta(&self, py: Python<'_>) -> PyResult<PyObject> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        let raw: HashMap<String, String> = redis::cmd("HGETALL").arg("vault:meta").query(&mut conn).map_err(to_py)?;
        let mut out: HashMap<String, (String, String)> = HashMap::new();
        for (v, js) in raw {
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&js) {
                let q = val.get("quote").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let b = val.get("base").and_then(|x| x.as_str()).unwrap_or("").to_string();
                out.insert(v, (q, b));
            }
        }
        Ok(pyo3::conversion::IntoPy::into_py(out, py))
    }

    fn upsert_vault_bins(&self, vault: String, bucket: u64, points: Vec<(u64,i128,i128,i128,i128)>) -> PyResult<()> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        let key = format!("vault:bins:{}:{}", vault.to_lowercase(), bucket);
        let mut pipe = redis::pipe();
        pipe.atomic();
        for (ts, mon, q, b, sh) in points {
            let packed = rmp_serde::to_vec(&(mon, q, b, sh)).map_err(to_py)?;
            pipe.hset(&key, ts.to_string(), packed);
        }
        pipe.query(&mut conn).map_err(to_py)?;
        Ok(())
    }

    fn load_vault_bins(&self, py: Python<'_>, vault: String) -> PyResult<PyObject> {
        let client = redis::Client::open(self.url.as_str()).map_err(to_py)?;
        let mut conn = client.get_connection().map_err(to_py)?;
        let pattern = format!("vault:bins:{}:*", vault.to_lowercase());
        let mut cursor: u64 = 0;
        let mut out: HashMap<u64, Vec<(u64,i128,i128,i128,i128)>> = HashMap::new();
        loop {
            let res: (u64, Vec<String>) = redis::cmd("SCAN").arg(cursor).arg("MATCH").arg(&pattern).arg("COUNT").arg(500).query(&mut conn).map_err(to_py)?;
            cursor = res.0;
            for key in res.1 {
                if let Some(bk) = key.rsplit(':').next().and_then(|s| s.parse::<u64>().ok()) {
                    let rows: HashMap<String, Vec<u8>> = redis::cmd("HGETALL").arg(&key).query(&mut conn).map_err(to_py)?;
                    let mut items: Vec<(u64,i128,i128,i128,i128)> = Vec::new();
                    for (ts, blob) in rows {
                        if let Ok((mon, q, b, sh)) = rmp_serde::from_slice::<(i128,i128,i128,i128)>(&blob) {
                            if let Ok(tsn) = ts.parse::<u64>() {
                                items.push((tsn, mon, q, b, sh));
                            }
                        }
                    }
                    items.sort_by_key(|x| x.0);
                    out.insert(bk, items);
                }
            }
            if cursor == 0 { break; }
        }
        Ok(pyo3::conversion::IntoPy::into_py(out, py))
    }
}

fn now_ts() -> i64 {
    (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64)
}

fn to_py<E: std::fmt::Display>(e: E) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
}

#[pymodule]
fn storage_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Store>()?;
    Ok(())
}
