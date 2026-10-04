// ZIP archive writer. Entries are deflated when that makes them smaller (STL and PDF text shrink by roughly half)
// and stored otherwise. Synchronous, so it runs the same in a browser, a worker and Node.
import {deflateSync,inflateSync} from 'fflate';
const CRC_TABLE=(()=>{const t=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=c&1?0xedb88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})();
export function crc32(data){let c=0xffffffff;for(let i=0;i<data.length;i++)c=CRC_TABLE[(c^data[i])&0xff]^(c>>>8);return(c^0xffffffff)>>>0;}
export function zip(entries){
 const enc=new TextEncoder(),locals=[],central=[];let offset=0;
 for(const entry of entries){
  const name=enc.encode(entry.name),data=typeof entry.data==='string'?enc.encode(entry.data):new Uint8Array(entry.data),crc=crc32(data);
  const packed=data.length>64?deflateSync(data,{level:6}):null,deflated=packed&&packed.length<data.length,body=deflated?packed:data,method=deflated?8:0;
  const head=new Uint8Array(30+name.length),h=new DataView(head.buffer);
  h.setUint32(0,0x04034b50,true);h.setUint16(4,20,true);h.setUint16(8,method,true);h.setUint16(12,33,true);h.setUint32(14,crc,true);h.setUint32(18,body.length,true);h.setUint32(22,data.length,true);h.setUint16(26,name.length,true);head.set(name,30);
  const dir=new Uint8Array(46+name.length),d=new DataView(dir.buffer);
  d.setUint32(0,0x02014b50,true);d.setUint16(4,20,true);d.setUint16(6,20,true);d.setUint16(10,method,true);d.setUint16(14,33,true);d.setUint32(16,crc,true);d.setUint32(20,body.length,true);d.setUint32(24,data.length,true);d.setUint16(28,name.length,true);d.setUint32(42,offset,true);dir.set(name,46);
  locals.push(head,body);central.push(dir);offset+=head.length+body.length;
 }
 const size=central.reduce((s,x)=>s+x.length,0),end=new Uint8Array(22),e=new DataView(end.buffer);
 e.setUint32(0,0x06054b50,true);e.setUint16(8,entries.length,true);e.setUint16(10,entries.length,true);e.setUint32(12,size,true);e.setUint32(16,offset,true);
 return new Blob([...locals,...central,end],{type:'application/zip'});
}
// Read every entry back (stored or deflated). Used by tests and by anything that needs to inspect a kit.
export async function unzip(blob){
 const buf=new Uint8Array(await blob.arrayBuffer()),view=new DataView(buf.buffer,buf.byteOffset,buf.byteLength),dec=new TextDecoder(),files={};let p=0;
 while(p+4<=buf.length&&view.getUint32(p,true)===0x04034b50){const method=view.getUint16(p+8,true),size=view.getUint32(p+18,true),n=view.getUint16(p+26,true),extra=view.getUint16(p+28,true),name=dec.decode(buf.subarray(p+30,p+30+n)),body=buf.subarray(p+30+n+extra,p+30+n+extra+size);files[name]=method===8?inflateSync(body):body.slice();p+=30+n+extra+size;}
 return files;
}
