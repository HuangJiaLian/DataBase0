from qutip import *
import numpy as np
import matplotlib.pyplot as plt
import scipy.optimize as optimize
from qutip.piqs import *
import multiprocess as mp
plt.rc("font",family="YouYuan",size="14") 
# plt.style.use('_mpl-gallery')#mini版的图
chi = 1
r = 0.01# np.pi / 4
N = 10
j = N//2        
ket = spin_coherent(j, np.pi / 2, 0)#初态在正上方
rho = ket2dm(ket)
Jp = jmat(j, "+")
J_ = jmat(j, "-")
Jz = jmat(j, "z")
Jy = jmat(j, "y")
Jx = jmat(j, "x")
L = np.sin(r)*Jp + np.cos(r)*J_  
H = chi*Jz**2
# H = chi*(np.cos(theta)*Jx + np.sin(theta)*np.cos(zeta)*Jy + np.sin(theta)*np.sin(zeta)*Jz)**2
n_max = 100
tlist = np.linspace(0, np.pi, n_max)
result = mesolve(H, rho, tlist, [], []) 

t = 20
def func(alpha, beta): 
    Jn = np.sin(alpha)*np.cos(beta)*Jx + np.sin(alpha)*np.sin(beta)*Jy + np.cos(alpha)*Jz        
    f = 4*variance(Jn, result.states[t])
    return f

parallel_map(func, list(np.linspace(0, np.pi, 100)), task_args=(np.array(list(np.linspace(0, 2*np.pi, 100))),))
# funlist = list(map(func1, t))    
label_size = 20   
fig, ax = plt.subplots()#figsize=(12,3) 
# # plt.ylim(0, 1.0)
# # plt.grid() #网格

plt.plot(t, funlist, '-', label = 'Q', color = 'C0', lw = 1.8) 
plt.ylabel("QFI", fontsize = label_size, color = '#FF4500')
plt.xlabel("t", fontsize = label_size, color = '#FF4500')
plt.title('N = 10, r =  0.62', fontsize = label_size, color = '#FF4500')
plt.legend( fontsize = 0.8 * label_size, loc = 'center right') 
plt.show()
