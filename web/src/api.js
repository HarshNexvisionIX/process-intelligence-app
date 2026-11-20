// All calls go to the local Python API (app/api/server.py). No AI keys or model names are ever sent from here.
async function request(path, options = {}) {
  let response
  try { response = await fetch('/api' + path, options) }
  catch { throw new Error('The Python API is not running. Start it with: python -m app.api') }
  let data
  try { data = await response.json() } catch { data = {} }
  if (!response.ok) {
    const d = data.detail
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg || JSON.stringify(x)).join('; ') : 'Request failed')
  }
  return data
}
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) })
const enc = encodeURIComponent

export const api = {
  health: () => request('/health'),
  jobs: () => request('/jobs'),
  job: id => request(`/jobs/${id}`),
  removeJob: id => request(`/jobs/${id}`, { method: 'DELETE' }),
  upload: (files, datasetClass, proc = {}) => {
    const body = new FormData()
    for (const f of files) body.append('files', f)
    body.append('dataset_class', datasetClass)
    body.append('process', proc.process || '')
    body.append('new_process', proc.new_process || '')
    return request('/jobs', { method: 'POST', body })
  },
  fromPath: (path, datasetClass, proc = {}) => request('/jobs/from-path', json('POST', { path, dataset_class: datasetClass, process: proc.process || '', new_process: proc.new_process || '' })),
  appNames: (id, column, title, user) => request(`/jobs/${id}/apps?column=${enc(column)}${title ? '&title=' + enc(title) : ''}${user ? '&user=' + enc(user) : ''}`),
  saveAppNames: rows => request('/apps/map', json('PUT', { rows })),
  importAppNames: (file, confirm) => { const body = new FormData(); body.append('file', file); body.append('confirm', confirm ? 'true' : 'false'); return request('/apps/map/import', { method: 'POST', body }) },
  caseSteps: id => request(`/jobs/${id}/case-steps`),
  saveCaseSteps: (id, body) => request(`/jobs/${id}/case-steps`, json('PUT', body)),
  saveCaseFlow: (id, body) => request(`/jobs/${id}/case-flow`, json('PUT', body)),
  processRecords: key => request(`/processes/${enc(key)}/records`),
  profile: id => request(`/jobs/${id}/profile`),
  convertNocase: (id, body) => request(`/jobs/${id}/convert/nocase`, json('POST', body)),
  convertCase: (id, body) => request(`/jobs/${id}/convert/case`, json('POST', body)),
  processes: () => request('/processes'),
  processMap: key => request(`/processes/${enc(key)}/map`),
  process: key => request(`/processes/${enc(key)}`),
  savePack: (key, body) => request(`/processes/${enc(key)}/pack`, json('PUT', body)),
  approvePack: (key, body) => request(`/processes/${enc(key)}/pack/approve`, json('POST', body)),
  setProcess: (id, body) => request(`/jobs/${id}/process`, json('POST', body)),
  context: id => request(`/jobs/${id}/context`),
  addDocuments: (id, files, kind, recordedOn, roles, walkthrough) => {
    const body = new FormData()
    for (const f of files) body.append('files', f)
    body.append('kind', kind)
    if (recordedOn) body.append('recorded_on', recordedOn)
    body.append('roles', roles || '')
    body.append('walkthrough', walkthrough ? 'true' : 'false')
    return request(`/jobs/${id}/documents`, { method: 'POST', body })
  },
  confirmContext: (id, body) => request(`/jobs/${id}/context/confirm`, json('POST', body)),
  reopenContext: id => request(`/jobs/${id}/context/reopen`, json('POST')),
  aiPackages: (id, kind) => request(`/jobs/${id}/ai/${kind}`),
  aiApprove: (id, kind, body) => request(`/jobs/${id}/ai/${kind}/approve`, json('POST', body)),
  aiRevoke: (id, kind, task) => request(`/jobs/${id}/ai/${kind}/revoke/${task}`, json('POST')),
  docsPrepare: id => request(`/jobs/${id}/ai/docs/prepare`, json('POST')),
  docsSend: id => request(`/jobs/${id}/ai/docs/send`, json('POST')),
  run: (id, useAi) => request(`/jobs/${id}/run`, json('POST', { use_ai: useAi })),
  runStatus: id => request(`/jobs/${id}/run`),
  overview: id => request(`/jobs/${id}/overview`),
  insights: (id, days) => request(`/jobs/${id}/insights?min_active_days=${days || 5}`),
  map: (id, level, step) => request(`/jobs/${id}/map?level=${level}${step ? '&step=' + enc(step) : ''}`),
  stage: (id, step) => request(`/jobs/${id}/stage?step=${enc(step)}`),
  review: id => request(`/jobs/${id}/review`),
  accept: (id, body) => request(`/jobs/${id}/review/accept`, json('POST', body)),
  reject: (id, body) => request(`/jobs/${id}/review/reject`, json('POST', body)),
  assign: (id, body) => request(`/jobs/${id}/review/assign`, json('POST', body)),
  answer: (id, body) => request(`/jobs/${id}/questions/answer`, json('POST', body)),
  addQuestion: (id, question) => request(`/jobs/${id}/questions`, json('POST', { question })),
  reportPrepare: id => request(`/jobs/${id}/report/prepare`, json('POST')),
  reportGenerate: (id, useAi) => request(`/jobs/${id}/report/generate?use_ai=${useAi ? 'true' : 'false'}`, json('POST')),
  quality: id => request(`/jobs/${id}/quality`),
  runQuality: id => request(`/jobs/${id}/quality`, json('POST')),
  runAnalysis: id => request(`/jobs/${id}/analysis`, json('POST')),
  activities: id => request(`/jobs/${id}/activities`),
  acceptActivities: (id, body) => request(`/jobs/${id}/activities/accept`, json('POST', body)),
  rejectActivities: (id, body) => request(`/jobs/${id}/activities/reject`, json('POST', body)),
  order: id => request(`/jobs/${id}/order`),
  saveOrder: (id, body) => request(`/jobs/${id}/order`, json('PUT', body)),
  summary: id => request(`/jobs/${id}/summary`),
  caseResults: id => request(`/jobs/${id}/case-results`),
  readout: id => request(`/jobs/${id}/readout`),
  readoutPrepare: id => request(`/jobs/${id}/readout/prepare`, json('POST')),
  readoutGenerate: id => request(`/jobs/${id}/readout/generate`, json('POST')),
  aiMap: (id, scope, budget) => request(`/jobs/${id}/ai-map?budget=${enc(budget || 'compact')}${scope ? `&scope=${enc(scope)}` : ''}`),
  aiMapPrepare: (id, body) => request(`/jobs/${id}/ai-map/prepare`, json('POST', body)),
  aiMapGenerate: (id, body) => request(`/jobs/${id}/ai-map/generate`, json('POST', body)),
  stageAi: (id, step, budget) => request(`/jobs/${id}/stage-ai?step=${enc(step)}&budget=${enc(budget || 'compact')}`),
  stageAiPrepare: (id, body) => request(`/jobs/${id}/stage-ai/prepare`, json('POST', body)),
  stageAiGenerate: (id, body) => request(`/jobs/${id}/stage-ai/generate`, json('POST', body)),
  systemMap: (mode, jobId) => request(`/system-map?mode=${mode}${jobId ? '&job_id=' + jobId : ''}`),
}
export const fileUrl = (id, name) => `/api/jobs/${id}/${name}`
export const resultUrl = (id, name) => `/api/jobs/${id}/files/${enc(name)}`
export const libraryFileUrl = (key, path) => `/api/processes/${enc(key)}/library-file?path=${enc(path)}`
export const bundleUrl = key => `/api/processes/${enc(key)}/bundle.zip`

export function downloadJson(name, value) {
  downloadBlob(name, new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
}
export function downloadCsv(name, rows) {
  if (!rows || !rows.length) return
  const cols = Array.from(new Set(rows.flatMap(x => Object.keys(x))))
  const cell = v => '"' + String(v ?? '').replaceAll('"', '""') + '"'
  const content = [cols.map(cell).join(','), ...rows.map(r => cols.map(k => cell(r[k])).join(','))].join('\r\n')
  downloadBlob(name, new Blob([content], { type: 'text/csv;charset=utf-8' }))
}
function downloadBlob(name, blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = name; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 3000)
}
