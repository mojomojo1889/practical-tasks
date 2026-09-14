from io import BytesIO
from PIL import Image
from fastapi.testclient import TestClient
from app.main import app

def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json() if r.content else None

with TestClient(app) as c:
    me=ok(c.post('/api/auth/login',json={'email':'teacher@test.fi','password':'LongTeacherPass123'})); h={'X-CSRF-Token':me['csrf_token']}
    task=ok(c.post('/api/tasks',headers=h,json={'title':'Install rack server','category':'server','description':'Rack and cable server','location':'Campus A','room':'A203','scheduled_at':None,'due_at':None,'capacity':2,'is_open':True}),201)
    ok(c.post('/api/auth/logout',headers=h),204)
    ok(c.post('/api/auth/register',json={'name':'Student One','email':'student@test.fi','password':'StudentPass123','group_name':'ICT22','invite_code':'GROUP42'}),201)
    me=ok(c.post('/api/auth/login',json={'email':'student@test.fi','password':'StudentPass123'})); sh={'X-CSRF-Token':me['csrf_token']}
    en=ok(c.post(f"/api/tasks/{task['id']}/enroll",headers=sh),201)
    ok(c.post(f"/api/enrollments/{en['id']}/start",headers=sh))
    ok(c.patch(f"/api/enrollments/{en['id']}/note",headers=sh,json={'note':'Mounted rails and connected power.'}))
    b=BytesIO(); Image.new('RGB',(40,30),'teal').save(b,'JPEG'); b.seek(0)
    ok(c.post(f"/api/enrollments/{en['id']}/photos",headers=sh,files=[('files',('rack.jpg',b,'image/jpeg'))]),201)
    ok(c.post(f"/api/enrollments/{en['id']}/submit",headers=sh))
    mine=ok(c.get('/api/my-work')); assert mine[0]['status']=='ready' and len(mine[0]['photos'])==1
    ok(c.post('/api/auth/logout',headers=sh),204)
    me=ok(c.post('/api/auth/login',json={'email':'teacher@test.fi','password':'LongTeacherPass123'})); th={'X-CSRF-Token':me['csrf_token']}
    rows=ok(c.get('/api/admin/enrollments')); assert rows[0]['note'].startswith('Mounted')
    ok(c.patch(f"/api/admin/enrollments/{en['id']}/status",headers=th,json={'status':'completed'}))
    ok(c.post('/api/auth/logout',headers=th),204)
    me=ok(c.post('/api/auth/login',json={'email':'student@test.fi','password':'StudentPass123'}))
    mine=ok(c.get('/api/my-work')); assert mine[0]['status']=='completed' and mine[0]['completed_at']
print('integration: PASS')
