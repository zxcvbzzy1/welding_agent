import test from 'node:test'
import assert from 'node:assert/strict'
import { playbackSegments, samplePlayback } from './robotPlayback.js'
import { artifactEventNames, artifactIdentity } from './runtimeEvents.js'

const point = (q, t, min = 0) => ({ joints:Array(6).fill(q),time_s:t,minimum_duration_s:min,stage:'WELDING' })
const simulation={seams:[
  {id:'L1',points:[point(0,0),point(1,1),{joints:null},point(3,0),point(4,1)],segments:[{start:0,end:1,duration_s:1},{start:3,end:4,duration_s:1}]},
  {id:'L2',points:[point(8,0),point(9,1)],segments:[{start:0,end:1,duration_s:1}]},
  {id:'geometry',points:[],segments:[]},
]}

test('selects seams and preserves independent segments',()=>{
  assert.equal(playbackSegments(simulation).length,3)
  assert.equal(playbackSegments(simulation,'L1').length,2)
  assert.equal(playbackSegments(simulation,'geometry').length,0)
})
test('interpolates within valid segments and resets across failed points',()=>{
  const segments=playbackSegments(simulation)
  assert.equal(samplePlayback(segments,.5).joints[0],.5)
  assert.equal(samplePlayback(segments,1).joints[0],3)
  assert.equal(samplePlayback(segments,2).joints[0],8)
  assert.equal(samplePlayback(segments,3).joints[0],0)
})
test('non looping playback stops at the last point',()=>{
  const sample=samplePlayback(playbackSegments(simulation),10,false)
  assert.equal(sample.joints[0],9)
  assert.equal(sample.finished,true)
  assert.equal(sample.progress,1)
})
test('speed multipliers preserve joint velocity limits',()=>{
  const data={seams:[{id:'L',points:[point(0,0),point(1,1,.8)],segments:[{start:0,end:1,duration_s:1}]}]}
  assert.equal(playbackSegments(data,'',2)[0].duration,.8)
  assert.equal(playbackSegments(data,'',.5)[0].duration,2)
})
test('geometry only inputs have no fabricated motion',()=>{
  assert.equal(samplePlayback([],0),null)
})
test('motion phase follows the waypoint being approached, including retract',()=>{
  const data={seams:[{id:'L',points:[{...point(0,0),stage:'APPROACH'},point(1,1),{...point(2,2),stage:'RETRACT'}],segments:[{start:0,end:2,duration_s:2}]}]}
  assert.equal(samplePlayback(playbackSegments(data),1.5).stage,'RETRACT')
})
test('simulation artifacts are subscribed and deduplicate by snapshot URL',()=>{
  assert.ok(artifactEventNames.includes('artifacts.robot_simulation'))
  assert.equal(artifactIdentity({type:'robot_simulation',url:'/api/im/robot-simulations/a'}),'robot_simulation|url|/api/im/robot-simulations/a')
  assert.notEqual(artifactIdentity({type:'robot_simulation',url:'/a'}),artifactIdentity({type:'robot_simulation',url:'/b'}))
})
