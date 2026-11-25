/** The intake validator rejects this response before admitting a new Work. */
export function isNewIntakeValidationRejection(status:number,code:string):boolean {
 return status===422&&code==='VALIDATION_ERROR';
}

export function exceedsNewIntakeTextBudget(text:string,maxBytes:number):boolean {
 return new TextEncoder().encode(JSON.stringify(text.trim())).length>maxBytes;
}
