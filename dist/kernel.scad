// PETAL parametric geometry kernel, version 2.2
// Units: millimetres. Front surface z = r*r / (4*f).
// Custom center interface: 120 mm OD, 30 mm center bore, four M4
// clearance windows on a 60 mm keyed / 40 mm legacy bolt circle. Custom interface.
// Square polar clearance windows accept M4 hardware; use washers.
// Front stays parabolic. Optional two-plane rear includes matched fittings.
// Angled panels can include separate breakaway ribs in the same STL.
// F6 Render, then File > Export > STL. Select one part for printing.
// assembly is for inspection only. Recheck fit after editing parameters.

assert(diameter>=180&&diameter<=1200&&fd>=.25&&fd<=.8,"Dish dimensions out of supported range.");
assert(thickness>=1.6&&thickness<=6&&resolution>=2&&resolution<=10,"Thickness or mesh spacing out of range.");
assert(sectors==0||(sectors>=6&&sectors<=32&&sectors%2==0),"Use automatic or 6-32 even petals.");
assert(rear_style==0||rear_style==1,"Choose curved or two-facet rear.");
assert(facet_angle>=10&&facet_angle<=15,"Rear fold angle must be 10-15 degrees.");
assert(!(rear_style&&print_angle==0),"Faceted petals use diagonal printing.");
assert(rows>=0&&rows<=12&&floor(rows)==rows,"Use 0-12 whole rings.");
function zz(r)=r*r/(4*diameter*fd);
function seq(a,b,n)=[for(i=[0:n])a+(b-a)*i/n];
// Range-bisect numeric sort avoids deep recursion on repeated mesh indices.
function qsort(a)=len(a)<2?a:let(lo=min(a),hi=max(a),mid=(lo+hi)/2,p=mid<hi?mid:lo)lo==hi?[lo]:concat(qsort([for(x=a)if(x<=p)x]),qsort([for(x=a)if(x>p)x]));
function hh(r,a,w=4.6)=[r-w/2,r+w/2,a-w/(2*r)*180/PI,a+w/(2*r)*180/PI];
function legacy_tilespec(n,k,j)=let(w=(diameter/2-45)/k,b0=45+j*w,b1=45+(j+1)*w,rm=b0+w*.65,h=180/n)
 [b0+(j>0?gap/2:0),b1-(j<k-1?gap/2:0),-h+gap/(2*b0)*180/PI,h-gap/(2*b0)*180/PI,
 concat([hh(rm,-h+8/rm*180/PI),hh(rm,h-8/rm*180/PI),hh(j==0?52.5:b0+8,0)],j<k-1?[hh(b1-8,0)]:[]),0,thickness,rear_style?1:0,n,k,j];
function tilespec(n,k,j)=joint_style?jpanel(n,k,j):legacy_tilespec(n,k,j);
function smode(s)=len(s)>7?s[7]:0;
function fdata(s)=let(f=diameter*fd,xc=(s[0]+s[1])/2,base=atan(xc/(2*f)),ms=[tan(base-facet_angle/2),tan(base+facet_angle/2)],mins=[for(m=ms)let(q=m>=0?1:cos(s[3]),r=max(s[0],min(s[1],2*f*m*q)))r*r/(4*f)-m*(r*q-xc)])[min(mins)-thickness,xc,ms[0],ms[1]];
function fz(x,d)=d[0]+max(d[2]*(x-d[1]),d[3]*(x-d[1]));
function roof(s,x,y)=let(mode=smode(s),r=sqrt(x*x+y*y))mode==2?let(d=fdata(tilespec(s[8],s[9],s[10])),h=180/s[8])fz(cos(h)*x+sin(h)*abs(y),d)-.2:
 mode==3?let(w=(diameter/2-45)/s[9],rb=45+(s[10]+1)*w,d1=fdata(tilespec(s[8],s[9],s[10])),d2=fdata(tilespec(s[8],s[9],s[10]+1)),u=max(0,min(1,(r-(rb-gap/2))/gap)))(1-u)*fz(x,d1)+u*fz(x,d2)-.2:
 mode==4?let(d=fdata(tilespec(s[8],s[9],0)),xx=max([for(i=[0:s[8]-1])cos(i*360/s[8])*x+sin(i*360/s[8])*y]))fz(xx,d)-.2:zz(r)+s[5];
function creases(s)=let(mode=smode(s))mode==1?[[1,0,fdata(s)[1]]]:mode==2?let(d=fdata(tilespec(s[8],s[9],s[10])),h=180/s[8])[[0,1,0],[cos(h),sin(h),d[1]],[cos(h),-sin(h),d[1]]]:mode==3?[[1,0,fdata(tilespec(s[8],s[9],s[10]))[1]],[1,0,fdata(tilespec(s[8],s[9],s[10]+1))[1]]]:mode==4?[for(i=[0:s[8]/2-1])let(a=(i+.5)*360/s[8])[-sin(a),cos(a),0]]:[];
function orient(v,t)=[cos(t)*v[0]+sin(t)*v[2],v[1],-sin(t)*v[0]+cos(t)*v[2]];
function yawpoint(v,a)=[cos(a)*v[0]-sin(a)*v[1],sin(a)*v[0]+cos(a)*v[1],v[2]];
function envelope(s,t,a)=let(vs=[for(r=seq(s[0],s[1],16),ang=seq(s[2],s[3],24),l=[0,1])yawpoint(orient([r*cos(ang),r*sin(ang),(l?(rear_style?fz(r*cos(ang),fdata(s)):zz(r)-thickness)-(joint_style?3:0):zz(r))],t),a)])
 [for(k=[0:2])max([for(v=vs)v[k]])-min([for(v=vs)v[k]])+(supports?(k==2?5.6:8):0)];
function fits(d)=((d[0]<=bed_x-2*margin&&d[1]<=bed_y-2*margin)||(d[1]<=bed_x-2*margin&&d[0]<=bed_y-2*margin))&&d[2]<=bed_z-2;
function exactfit(d)=d[0]<=bed_x-2*margin&&d[1]<=bed_y-2*margin&&d[2]<=bed_z-2;
function anglechoice(s,angle)=let(t=atan((s[0]+s[1])/(4*diameter*fd))-angle,choices=[for(a=angle==0?[0,90]:[0:15:90])let(d=envelope(s,t,a))if(exactfit(d))[d[0]*d[1]+a*.001,angle,a,t]])len(choices)==0?[]:[for(c=choices)if(c[0]==min([for(v=choices)v[0]]))c][0];
function pickprint(s,i=0)=let(angles=print_angle==-1?((supports||rear_style||joint_style)?[60,55,50,45,65]:[0]):[print_angle])i>=len(angles)?[]:let(c=anglechoice(s,angles[i]))len(c)>0?c:pickprint(s,i+1);
function valid(n,k)=(diameter/2-45)/k>=40&&(!joint_style||jfit(n,k))&&min([for(j=[0:k-1])len(pickprint(tilespec(n,k,j)))>0?1:0])==1;
assert(supports==0||supports==1,"Supports must be 0 or 1.");
assert(print_angle==-1||print_angle==0||(print_angle>=45&&print_angle<=70),"Use auto, low profile, or 45-70 degrees.");
assert(!(supports&&print_angle==0),"Ribs require an angled panel.");
assert(rib_count>=2&&rib_count<=5&&floor(rib_count)==rib_count,"Use 2-5 whole ribs.");
assert(contact_gap>=.1&&contact_gap<=.4&&contact_width>=.4&&contact_width<=.8&&rib_pitch>=6&&rib_pitch<=20,"Support settings out of range.");
candidates=[for(k=rows==0?[1:12]:[rows],n=sectors==0?[6:2:32]:[sectors])if(valid(n,k))[n*k+k*.15+n*.001,n,k]];
assert(len(candidates)>0,"No segmentation fits. Change bed or dish settings.");
assert(fits([120,120,18]),"Center clamp does not fit bed.");
choice=[for(c=candidates)if(c[0]==min([for(t=candidates)t[0]]))c][0];
N=choice[1]; K=choice[2]; W=(diameter/2-45)/K;
echo(petals=N,rings=K,depth=diameter/(16*fd),focus=diameter*fd);
function extra_r(s)=smode(s)==3?let(r=(s[0]+s[1])/2)[r-gap/2,r,r+gap/2]:[];
function rr(s)=qsort(concat(extra_r(s),seq(s[0],s[1],max(1,ceil((s[1]-s[0])/resolution))),[for(h=s[4],r=[h[0],h[1]])if(r>s[0]&&r<s[1])r]));
function aa(s)=qsort(concat(seq(s[2],s[3],max(2,ceil((s[3]-s[2])*PI/180*s[1]/resolution))),[for(h=s[4],a=[h[2],h[3]])if(a>s[2]&&a<s[3])a]));
function inside(r,a,hs)=len([for(h=hs)if(r>h[0]&&r<h[1]&&a>h[2]&&a<h[3])1])>0;
function on(i,j,R,A,hs,full)=let(jj=full?(j+len(A)-1)%(len(A)-1):j)i>=0&&i<len(R)-1&&jj>=0&&jj<len(A)-1?!inside((R[i]+R[i+1])/2,(A[jj]+A[jj+1])/2,hs):false;
function vid(i,j,l,nr,nc)=l*nr*nc+i*nc+j%nc;
function verts(s)=let(R=rr(s),A=aa(s),full=abs(s[3]-s[2]-360)<.00001,nc=len(A)-(full?1:0))
 [for(l=[0:1],r=R,j=[0:nc-1])[r*cos(A[j]),r*sin(A[j]),l]];
function faces(s)=let(R=rr(s),A=aa(s),nr=len(R),full=abs(s[3]-s[2]-360)<.00001,nc=len(A)-(full?1:0),off=nr*nc)
 [for(i=[0:nr-2],j=[0:len(A)-2])if(on(i,j,R,A,s[4],full))let(q=[vid(i,j,0,nr,nc),vid(i+1,j,0,nr,nc),vid(i+1,j+1,0,nr,nc),vid(i,j+1,0,nr,nc)],b=[for(x=q)x+off])each concat(
 [[q[0],q[1],q[2]],[q[0],q[2],q[3]],[b[2],b[1],b[0]],[b[3],b[2],b[0]]],
 [for(e=[[0,1,i,j-1],[1,2,i+1,j],[2,3,i,j+1],[3,0,i-1,j]])if(!on(e[2],e[3],R,A,s[4],full))each [[q[e[0]],b[e[0]],b[e[1]]],[q[e[0]],b[e[1]],q[e[1]]]]] )];
// Edge-indexed triangle splitting preserves watertight crease topology.
function crossing(a,b)=(a>1e-6&&b< -1e-6)||(a< -1e-6&&b>1e-6);
function ekey(u,v,n)=min(u,v)*n+max(u,v);
function bfind(a,x,lo=0,hi=undef)=let(h=is_undef(hi)?len(a)-1:hi,m=floor((lo+h)/2))lo>h?-1:a[m]==x?m:a[m]<x?bfind(a,x,m+1,h):bfind(a,x,lo,m-1);
function clipface(f,d,sgn,keys,n)=[for(j=[0:2])let(u=f[j],v=f[(j+1)%3])each concat(sgn*d[u]>=-1e-6?[u]:[],crossing(d[u],d[v])?[n+bfind(keys,ekey(u,v,n))]:[])];
function fan(poly)=len(poly)<3?[]:[for(i=[1:len(poly)-2])[poly[0],poly[i],poly[i+1]]];
function splitstep(M,line)=let(V=M[0],F=M[1],n=len(V),d=[for(v=V)line[0]*v[0]+line[1]*v[1]-line[2]],keys=qsort([for(f=F,j=[0:2])let(u=f[j],v=f[(j+1)%3])if(crossing(d[u],d[v]))ekey(u,v,n)]),extra=[for(key=keys)let(u=floor(key/n),v=key%n,t=d[u]/(d[u]-d[v]))V[u]+(V[v]-V[u])*t],F2=[for(f=F)each ((min([for(i=f)d[i]])>=-1e-6||max([for(i=f)d[i]])<=1e-6)?[f]:concat(fan(clipface(f,d,1,keys,n)),fan(clipface(f,d,-1,keys,n))))])[concat(V,extra),F2];
function splitall(M,ls,i=0)=i>=len(ls)?M:splitall(splitstep(M,ls[i]),ls,i+1);
function legacy_modelmesh(s)=let(M=splitall([verts(s),faces(s)],creases(s)),tops=[for(v=M[0])roof(s,v[0],v[1])],floor=min(tops)-s[6],mode=smode(s),d=mode==1?fdata(s):[]) [[for(i=[0:len(M[0])-1])let(v=M[0][i],bottom=mode==1?fz(v[0],d):mode>=2?floor:tops[i]-s[6])[v[0],v[1],tops[i]*(1-v[2])+bottom*v[2]]],M[1]];
// Analytic rear envelope lies beneath the tessellated shell. A curvature
// allowance keeps interpolated rib ridges below that envelope.
function domain(y,s,t,cx)=abs(y)>=s[1]?[]:let(lo=max(sqrt(max(0,s[0]*s[0]-y*y)),abs(y)/tan(s[3])),hi=sqrt(s[1]*s[1]-y*y))hi<=lo?[]:[for(x=[lo,hi])cos(t)*x+sin(t)*(rear_style?fz(x,fdata(s)):(x*x+y*y)/(4*diameter*fd)-thickness)-cx];
function under(x,y,s,t,cx,zmin)=let(d=domain(y,s,t,cx))len(d)==0?1e99:(x<d[0]-.0000001||x>d[1]+.0000001)?1e99:rear_style?let(fd=fdata(s),hinge=cos(t)*fd[1]+sin(t)*fd[0]-cx,m=x<=hinge?fd[2]:fd[3],xx=(x+cx-sin(t)*(fd[0]-m*fd[1]))/(cos(t)+sin(t)*m))-sin(t)*xx+cos(t)*fz(xx,fd)-zmin+5.6:let(A=sin(t)/(4*diameter*fd),B=cos(t),C=sin(t)*(y*y/(4*diameter*fd)-thickness)-(x+cx),disc=B*B-4*A*C,xx=abs(A)<1e-12?-C/B:-2*C/(B+sqrt(max(0,disc))))disc<0?1e99:-sin(t)*xx+cos(t)*((xx*xx+y*y)/(4*diameter*fd)-thickness)-zmin+5.6;
function ribdrop(x,a)=let(phase=((x-a)%rib_pitch+rib_pitch)%rib_pitch)phase<=1.6?0:phase<2.6?(phase-1.6)*.8:phase<=rib_pitch-1?.8:(rib_pitch-phase)*.8;
function ribverts(xs,hs,y,foot)=[for(i=[0:len(xs)-1])let(h=hs[i])each [for(p=[[-foot/2,0],[foot/2,0],[foot/2,1.2],[.6,2],[.6,h-.8],[contact_width/2,h],[-contact_width/2,h],[-.6,h-.8],[-.6,2],[-foot/2,1.2]])[xs[i],y+p[0],p[1]]]];
module rib(s,t,cx,zmin,y,foot){probes=[y-.6,y,y+.6];ds=[for(yy=probes)let(d=domain(yy,s,t,cx))if(len(d)>0)d];a=min([for(d=ds)d[0]]);b=max([for(d=ds)d[1]]);
 xs=qsort([for(x=concat(seq(a,b,ceil(b-a)),[for(d=ds)each d],rear_style?let(fd=fdata(s),x=cos(t)*fd[1]+sin(t)*fd[0]-cx)(x>a&&x<b?[x]:[]):[],[for(i=[0:ceil((b-a)/rib_pitch)],u=[0,1.6,2.6,rib_pitch-1,rib_pitch])let(x=a+i*rib_pitch+u)if(x>a&&x<b)x]))round(x*1e9)/1e9]);
 derivative=rear_style?min([for(m=[fdata(s)[2],fdata(s)[3]])cos(t)+sin(t)*m]):cos(t)+min(0,sin(t)*s[1]/(2*diameter*fd));assert(derivative>.05,"Underside too steep for ribs; lower angle.");safety=rear_style?.025:1/(16*diameter*fd*pow(derivative,3))+.025;
 hs=[for(x=xs)let(low=min([for(yy=probes)under(x,yy,s,t,cx,zmin)]))low<1e98?max(3,low-contact_gap-ribdrop(x,a)-safety):3];
 V=concat(ribverts(xs,hs,y,foot),[[xs[0],y,1],[xs[len(xs)-1],y,1]]);M=len(xs)*10;
 F=concat([for(i=[0:len(xs)-2],j=[0:9])let(a=i*10+j,b=i*10+(j+1)%10,c=b+10,d=a+10)each [[a,b,c],[a,c,d]]],[for(j=[0:9])each [[M,(j+1)%10,j],[M+1,(len(xs)-1)*10+j,(len(xs)-1)*10+(j+1)%10]]]);
 polyhedron(points=V,faces=[for(f=F)[f[2],f[1],f[0]]],convexity=12);
}
module patch(s,t=0,printing=false,is_panel=false,bed_yaw=0){M=modelmesh(s);V=M[0];F=M[1];Vt=[for(v=V)orient(v,t)];lo=[for(k=[0:2])min([for(v=Vt)v[k]])];hi=[for(k=[0:2])max([for(v=Vt)v[k]])];
 Vp=[for(v=Vt)[v[0]-(lo[0]+hi[0])/2,v[1]-(lo[1]+hi[1])/2,v[2]-lo[2]+(is_panel&&supports?5.6:0)]];
 swap=hi[0]-lo[0]>bed_x-2*margin||hi[1]-lo[1]>bed_y-2*margin;
 rot=is_panel?bed_yaw:(swap?90:0);
 polyhedron(points=printing?[for(v=Vp)yawpoint(v,rot)]:V,faces=[for(f=F)[f[2],f[1],f[0]]],convexity=12);
 if(printing&&is_panel&&supports){tip=s[0]*sin(s[3]);foot=min(8,2*tip/(rib_count-1)*.75);for(y=seq(-tip,tip,rib_count-1))rotate([0,0,rot])color("Orange")if(joint_style)jrib(s,Vp,y,foot);else rib(s,t,(lo[0]+hi[0])/2,lo[2],y,foot);}
}
function legacy_side(j)=let(r=45+j*W+W*.65)[r-6,r+6,-14/r*180/PI,14/r*180/PI,[hh(r,-8/r*180/PI),hh(r,8/r*180/PI)],-thickness-.2,3.2,rear_style?2:0,N,K,j];
function legacy_radial(j)=let(r=45+(j+1)*W)[r-14,r+14,-6/r*180/PI,6/r*180/PI,[hh(r-8,0),hh(r+8,0)],-thickness-.2,3.2,rear_style?3:0,N,K,j];
function root_holes()=[for(i=[0:N-1])hh(52.5,i*360/N)];
function legacy_rear()=[15,60,-180/N,360-180/N,concat(root_holes(),[for(i=[0:3])hh(20,45+i*90)]),-thickness-.2,6,rear_style?4:0,N,K,0];
function clamp()=[joint_style?47:43,joint_style?58:60,-180/N,360-180/N,root_holes(),joint_style?3:3.4,joint_style?2.8:3.2];
module single(type,j=0,printing=true,q=0){s=type=="panel"?tilespec(N,K,j):type=="side-bridge"?side(j,q):type=="ring-bridge"?radial(j,q):type=="hub-rear"?rear():clamp();cp=type=="panel"&&printing?pickprint(s):[];t=type=="panel"&&printing?cp[3]:(type=="side-bridge"||type=="ring-bridge")&&!rear_style&&!joint_style?atan((s[0]+s[1])/(4*diameter*fd)):0;if(type=="panel"&&printing)echo(print_angle=cp[1],bed_rotation=cp[2],support_ribs=supports?rib_count:0);patch(s,t,printing,type=="panel",type=="panel"&&printing?cp[2]:0);}
if(part=="assembly"){
 for(j=[0:K-1],i=[0:N-1])rotate([0,0,i*360/N+ring_phase(N,j)]){
  color(j%2==0?"Teal":"LightSeaGreen")single("panel",j,false);
  for(q=[0:(joint_style?jstation(N,K,j)[4]:1)-1])rotate([0,0,180/N])color("SlateGray")single("side-bridge",j,false,q);
  if(j<K-1)for(q=[0:len(radial_angles(N,K,j))-1])rotate([0,0,radial_angles(N,K,j)[q]])color("SlateGray")single("ring-bridge",j,false,q);
 }
 color("Orange")single("hub-rear",0,false);color("Orange")single("hub-clamp",0,false);
}else {assert(part=="panel"||part=="side-bridge"||part=="ring-bridge"||part=="hub-rear"||part=="hub-clamp","Unknown part");assert(ring>=1&&ring<=K,"Ring out of range");assert(part!="ring-bridge"||ring<K,"Last ring has no outward bridge");assert(station>=1&&floor(station)==station,"Use a whole station number starting at one");assert(part!="side-bridge"||station<=(joint_style?jstation(N,K,ring-1)[4]:1),"Side station out of range");assert(part!="ring-bridge"||station<=len(radial_angles(N,K,ring-1)),"Ring station out of range");single(part,ring-1,true,station-1);}

assert(adaptive_joints==0||adaptive_joints==1,"Use on/off adaptive joints");
assert(stagger_rings==0||stagger_rings==1,"Use on/off staggered rings");
assert(connector_spacing>=60&&connector_spacing<=180,"Connector spacing must be 60-180 mm");
// Keyed rear docking system. 8 mm shoulder tongues; rear sockets use
// joint_clearance per side. 3 mm local pads preserve reflector wall thickness.
assert(joint_style==0||joint_style==1,"Choose legacy or keyed joints.");
assert(joint_clearance>=.1&&joint_clearance<=.4,"Joint clearance must be .1-.4 mm per side.");
function kd()=2.6;
function ring_phase(n,j)=joint_style&&stagger_rings?(j%2)*180/n:0;
function ring_bolt_span()=adaptive_joints||stagger_rings?7:8;
function jstation(n,k,j,q=0)=let(w=(diameter/2-45)/k,b0=45+j*w,b1=b0+w,nl=joint_style&&(adaptive_joints||stagger_rings),lo=nl?max(j>0?b0+16:62,16*n/PI):(j>0?b0+2:62),hi=b1-(nl&&j<k-1?16:2),count=joint_style&&adaptive_joints?max(1,ceil((hi-lo)/connector_spacing)):1,cell=(hi-lo)/count)[lo+(q+.5)*cell,min(9,cell/2-6),b0,b1,count];
function radial_angles(n,k,j)=!joint_style?[0]:let(r=45+(j+1)*(diameter/2-45)/k,h=180/n,width=stagger_rings?h:2*h,bases=stagger_rings?[-h/2,h/2]:[0],count=adaptive_joints?max(1,ceil(r*width*PI/180/connector_spacing)):1)[for(b=bases,i=[0:count-1])b+((i+.5)/count-.5)*width];
function jcenters(n,k,j)=let(t=jstation(n,k,j),h=180/n)concat([for(q=[0:t[4]-1])let(u=jstation(n,k,j,q))for(r=[u[0]],sg=[-1,1])[r,sg*(h-8/r*180/PI)]],j==0?[[52.5,0]]:[for(a=radial_angles(n,k,j-1),sg=[-1,1])[t[2]+8,a+sg*ring_bolt_span()/t[2]*180/PI]],j<k-1?[for(a=radial_angles(n,k,j),sg=[-1,1])[t[3]-8,a+sg*ring_bolt_span()/t[3]*180/PI]]:[]);
function jboxes(cs,w)=[for(c=cs)hh(c[0],c[1],w)];
function jfit(n,k)=min([for(j=[0:k-1])let(t=jstation(n,k,j),s=legacy_tilespec(n,k,j),ps=jboxes(jcenters(n,k,j),12))t[1]>=5&&len([for(b=ps)if(b[0]<s[0]+.5||b[1]>s[1]-.5||b[2]<s[2]+.001*180/PI||b[3]>s[3]-.001*180/PI)1])==0&&len([for(i=[0:len(ps)-1],u=[0:len(ps)-1])if(i<u&&min(ps[i][1],ps[u][1])>max(ps[i][0],ps[u][0])+.001&&min(ps[i][3],ps[u][3])>max(ps[i][2],ps[u][2])+.001*180/PI)1])==0?1:0])==1;
function jpanel(n,k,j)=let(s=legacy_tilespec(n,k,j))[s[0],s[1],s[2],s[3],jboxes(jcenters(n,k,j),4.6),0,thickness,5,n,k,j];
function side(j,q=0)=joint_style?let(t=jstation(N,K,j,q),r=t[0],d=t[1])[r-6,r+6,-14/r*180/PI,14/r*180/PI,[],0,4.5,6,N,K,j,q]:legacy_side(j);
function radial(j,q=0)=joint_style?let(r=45+(j+1)*W)[r-14,r+14,(-ring_bolt_span()/r-6/(r-8))*180/PI,(ring_bolt_span()/r+6/(r-8))*180/PI,[],0,4.5,7,N,K,j,q]:legacy_radial(j);
function rear()=joint_style?[15,60,-180/N,360-180/N,[],0,6,8,N,K,0]:legacy_rear();
function jc(s)=smode(s)==5?jcenters(s[8],s[9],s[10]):smode(s)==6?let(t=jstation(s[8],s[9],s[10],s[11]))[for(r=[t[0]],sg=[-1,1])[r,sg*8/r*180/PI]]:smode(s)==7?let(r=45+(s[10]+1)*(diameter/2-45)/s[9])[for(rr=[r-8,r+8],sg=[-1,1])[rr,sg*ring_bolt_span()/r*180/PI]]:[for(i=[0:s[8]-1])[52.5,i*360/s[8]]];
function jholes(s)=concat(jboxes(jc(s),4.6),smode(s)==8?[for(i=[0:3])hh(30,45+i*90)]:[]);
function jallboxes(s)=concat(jholes(s),jboxes(jc(s),smode(s)==5?8+2*joint_clearance:8),smode(s)==5?jboxes(jc(s),12):[]);
function jalpha(s)=radial_angles(s[8],s[9],s[10])[s[11]];
function jbeta(s)=jalpha(s)-(stagger_rings?sign(jalpha(s))*180/s[8]:0);
function jrear(s,x,y)=let(mode=smode(s),r=sqrt(x*x+y*y),sp=legacy_tilespec(s[8],s[9],s[10]))!rear_style?zz(r)-thickness:mode==5?fz(x,fdata(sp)):mode==6?let(h=180/s[8])fz(cos(h)*x+sin(h)*abs(y),fdata(sp)):mode==7?let(rb=45+(s[10]+1)*(diameter/2-45)/s[9],d2=fdata(legacy_tilespec(s[8],s[9],s[10]+1)),u=max(0,min(1,(r-rb+gap/2)/gap)))(1-u)*fz(cos(jalpha(s))*x-sin(jalpha(s))*y,fdata(sp))+u*fz(cos(jbeta(s))*x-sin(jbeta(s))*y,d2):fz(max([for(i=[0:s[8]-1])cos(i*360/s[8])*x+sin(i*360/s[8])*y]),fdata(sp));
function jlevel(s,x,y,l,floor)=smode(s)==5?(l==0?jrear(s,x,y)-3:l==1?jrear(s,x,y)-.4:l==2?jrear(s,x,y):zz(sqrt(x*x+y*y))):(l==0?floor:l==1?jrear(s,x,y)-3-.2:jrear(s,x,y)-.6);
function jcreases(s)=let(mode=smode(s),d=fdata(legacy_tilespec(s[8],s[9],s[10])),h=180/s[8])!rear_style?[]:mode==5?[[1,0,d[1]]]:mode==6?[[0,1,0],[cos(h),sin(h),d[1]],[cos(h),-sin(h),d[1]]]:mode==7?[[cos(jalpha(s)),-sin(jalpha(s)),d[1]],[cos(jbeta(s)),-sin(jbeta(s)),fdata(legacy_tilespec(s[8],s[9],s[10]+1))[1]]]:[for(i=[0:s[8]/2-1])let(a=(i+.5)*360/s[8])[-sin(a),cos(a),0]];
function jrr(s)=qsort(concat(seq(s[0],s[1],max(1,ceil((s[1]-s[0])/resolution))),[for(b=jallboxes(s),r=[b[0],b[1]])if(r>s[0]&&r<s[1])r],smode(s)==7?let(r=(s[0]+s[1])/2)[r-gap/2,r,r+gap/2]:[]));
function jaa(s)=qsort(concat(seq(s[2],s[3],max(2,ceil((s[3]-s[2])*PI/180*s[1]/resolution))),[for(b=jallboxes(s),a=[b[2],b[3]])if(a>s[2]&&a<s[3])a]));
function jon(s,i,j,l,R,A,full)=let(jj=full?(j+len(A)-1)%(len(A)-1):j,nl=smode(s)==5?3:2)l<0||l>=nl||i<0||i>=len(R)-1||jj<0||jj>=len(A)-1?false:let(r=(R[i]+R[i+1])/2,a=(A[jj]+A[jj+1])/2,cs=jc(s))smode(s)==5?((l==2||inside(r,a,jboxes(cs,12)))&&!inside(r,a,l==0?jboxes(cs,8+2*joint_clearance):jholes(s))):((l==0||inside(r,a,jboxes(cs,8)))&&!inside(r,a,jholes(s)));
function jvirtual(s)=let(R=jrr(s),A=jaa(s),full=abs(s[3]-s[2]-360)<.00001,nr=len(R),nc=len(A)-(full?1:0),nl=smode(s)==5?3:2,off=nr*nc,V=[for(l=[0:nl],r=R,j=[0:nc-1])[r*cos(A[j]),r*sin(A[j]),l]],F=[for(l=[0:nl-1],i=[0:nr-2],j=[0:len(A)-2])if(jon(s,i,j,l,R,A,full))let(b=[vid(i,j,l,nr,nc),vid(i+1,j,l,nr,nc),vid(i+1,j+1,l,nr,nc),vid(i,j+1,l,nr,nc)],q=[for(x=b)x+off])each concat(!jon(s,i,j,l+1,R,A,full)?[[q[0],q[1],q[2]],[q[0],q[2],q[3]]]:[],!jon(s,i,j,l-1,R,A,full)?[[b[2],b[1],b[0]],[b[3],b[2],b[0]]]:[],[for(e=[[0,1,i,j-1],[1,2,i+1,j],[2,3,i,j+1],[3,0,i-1,j]])if(!jon(s,e[2],e[3],l,R,A,full))each [[q[e[0]],b[e[0]],b[e[1]]],[q[e[0]],b[e[1]],q[e[1]]]]])])[V,F];
function jointmesh(s)=let(M=splitall(jvirtual(s),jcreases(s)),nl=smode(s)==5?3:2,floor=min([for(v=M[0])jrear(s,v[0],v[1])-3-.2])-s[6],V=[for(v=M[0])let(i=min(nl-1,floor(v[2])),t=v[2]-i)[v[0],v[1],jlevel(s,v[0],v[1],i,floor)*(1-t)+jlevel(s,v[0],v[1],i+1,floor)*t]],used=qsort([for(f=M[1])each f]))[[for(i=used)V[i]],[for(f=M[1])[for(i=f)bfind(used,i)]]];
function modelmesh(s)=smode(s)>=5?jointmesh(s):legacy_modelmesh(s);
function slicepts(vs,y)=[for(i=[0:2])let(a=vs[i],b=vs[(i+1)%3])each concat(abs(a[1]-y)<1e-8?[[a[0],a[2]]]:[],((a[1]<y&&b[1]>y)||(a[1]>y&&b[1]<y))?let(t=(y-a[1])/(b[1]-a[1]))[[a[0]+t*(b[0]-a[0]),a[2]+t*(b[2]-a[2])]]:[])];
function slices(V,F,y)=[for(f=F)let(ps=slicepts([for(i=f)V[i]],y))if(len(ps)>=2)let(xs=[for(v=ps)v[0]],a=[for(v=ps)if(v[0]==min(xs))v][0],b=[for(v=ps)if(v[0]==max(xs))v][0])if(b[0]-a[0]>1e-8)[a[0],b[0],a[1],(b[1]-a[1])/(b[0]-a[0])]];
function slicez(ss,x)=let(zs=[for(s=ss)if(x>=s[0]-2e-6&&x<=s[1]+2e-6)s[2]+(x-s[0])*s[3]])len(zs)?min(zs):1e99;
module jrib(s,V,y,foot){F=modelmesh(s)[1];ss=[for(d=[-.6,-.3,0,.3,.6])each slices(V,F,y+d)];ends=[for(v=ss)each [v[0],v[1]]];a=min(ends);b=max(ends);xs=qsort([for(x=concat(ends,seq(a,b,ceil(b-a)),[for(i=[0:ceil((b-a)/rib_pitch)],u=[0,1.6,2.6,rib_pitch-1,rib_pitch])let(x=a+i*rib_pitch+u)if(x>a&&x<b)x]))round(x*1e6)/1e6]);hs=[for(x=xs)let(z=slicez(ss,x))z<1e98?max(3,z-contact_gap-ribdrop(x,a)-.04):3];VV=concat(ribverts(xs,hs,y,foot),[[xs[0],y,1],[xs[len(xs)-1],y,1]]);L=len(xs)*10;FF=concat([for(i=[0:len(xs)-2],j=[0:9])let(a=i*10+j,b=i*10+(j+1)%10,c=b+10,d=a+10)each [[a,b,c],[a,c,d]]],[for(j=[0:9])each [[L,(j+1)%10,j],[L+1,(len(xs)-1)*10+j,(len(xs)-1)*10+(j+1)%10]]]);polyhedron(points=VV,faces=[for(f=FF)[f[2],f[1],f[0]]],convexity=12);}
